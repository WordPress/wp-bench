"""Model interface leveraging LiteLLM providers.

Model calls are wrapped in a bounded, configurable retry policy
(tenacity, exponential backoff with jitter). Only transient provider
failures are retried — rate limits, timeouts, connection drops, and
5xx/internal errors. Deterministic request errors (bad model name, bad
API key, context overflow, malformed request) fail fast so systemic
problems are not hidden behind retries.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from litellm import completion, completion_cost
from litellm.exceptions import (
    APIConnectionError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.utils import ModelResponse
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from .config import ModelConfig

#: Sampling parameters a provider may retire on a per-model basis. Dropping
#: one costs us determinism we never actually had (no provider guarantees
#: reproducibility at a fixed temperature) and is always preferable to
#: aborting the run, so these are recoverable rather than fatal.
_DROPPABLE_SAMPLING_PARAMS: tuple[str, ...] = ("temperature", "top_p", "top_k")

#: Exception types that indicate a transient provider problem.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    RateLimitError,
    Timeout,
    APIConnectionError,
    InternalServerError,
    ServiceUnavailableError,
)


@dataclass
class ModelGeneration:
    """A completion plus the call metadata needed for audit and reporting."""

    text: str
    raw_response: ModelResponse | None
    retry_count: int
    latency_ms: float
    provider_response_id: str | None
    #: True when ``temperature`` was omitted because the model rejects it.
    #: Kept as its own field because existing result records carry it;
    #: ``dropped_params`` is the general form.
    temperature_fallback: bool = False
    #: Every sampling parameter omitted from the successful call, whether
    #: dropped in response to this call's rejection or already known bad.
    dropped_params: tuple[str, ...] = field(default_factory=tuple)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    def usage_dict(self) -> dict[str, Any]:
        """Usage object in the canonical result-record shape."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": round(self.latency_ms, 1),
        }


class ModelInterface:
    """Thin wrapper over LiteLLM to keep prompts consistent."""

    def __init__(self, config: ModelConfig, system_prompt: str | None = None):
        self.config = config
        #: Sampling parameters this model has rejected. Learned on the first
        #: rejection and reused for the rest of the run, so a suite pays the
        #: failed call once per model rather than once per test.
        self._unsupported_params: set[str] = set()
        #: Variant state (e.g. injected skill content), deliberately not part
        #: of ModelConfig so it never lands in serialized model config blocks.
        self.system_prompt = system_prompt

    def generate(self, prompt: str) -> str:
        """Generate a completion for the given prompt (text only).

        Compatibility wrapper over generate_with_metadata().
        """
        return self.generate_with_metadata(prompt).text

    def generate_with_metadata(self, prompt: str) -> ModelGeneration:
        """Generate a completion with retry, latency, and call metadata.

        Transient provider errors (rate limit, timeout, connection, 5xx)
        are retried up to ``config.max_retries`` additional attempts with
        exponential backoff and jitter. Non-retryable errors propagate
        immediately, preserving the original exception for the caller.

        Latency covers all attempts, including backoff waits, because that
        is the wall-clock cost of obtaining the completion.
        """
        requested = self._requested_kwargs(prompt)
        kwargs = {k: v for k, v in requested.items() if k not in self._unsupported_params}
        already_dropped = [k for k in requested if k not in kwargs]
        started = time.perf_counter()
        attempt_count = 0
        newly_dropped: list[str] = []

        def _should_retry(error: BaseException) -> bool:
            if not self._retry_enabled_for(error):
                return False
            return isinstance(error, _TRANSIENT_ERRORS)

        def _before(retry_state: RetryCallState) -> None:
            nonlocal attempt_count
            attempt_count = retry_state.attempt_number

        response: ModelResponse | None = None
        for attempt in Retrying(
            stop=stop_after_attempt(self.config.max_retries + 1),
            wait=wait_random_exponential(
                multiplier=self.config.retry_min_seconds,
                max=self.config.retry_max_seconds,
            ),
            retry=retry_if_exception(_should_retry),
            before=_before,
            reraise=True,
        ):
            with attempt:
                # Bad requests are deterministic and fail fast, except for a
                # rejected sampling parameter, which we drop and re-send.
                # That fallback is intentional behavior, not a transient
                # retry, so it is flagged separately and not counted.
                response = self._complete_dropping_unsupported(kwargs, newly_dropped)

        assert response is not None  # Retrying(reraise=True) raises otherwise
        dropped_params = tuple(sorted(set(already_dropped) | set(newly_dropped)))
        latency_ms = (time.perf_counter() - started) * 1000
        choice = response.choices[0]
        usage = _extract_usage(response)
        return ModelGeneration(
            # Providers can return None content (safety block, thinking-only
            # response, truncation). That is a model output, not a harness
            # fault: normalize to "" so it grades as an empty answer.
            text=choice.message["content"] or "",  # type: ignore[union-attr, index]
            raw_response=response,
            retry_count=attempt_count - 1,
            latency_ms=latency_ms,
            provider_response_id=getattr(response, "id", None),
            temperature_fallback="temperature" in dropped_params,
            dropped_params=dropped_params,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            cost_usd=_estimate_cost_safe(response),
        )

    def _complete_dropping_unsupported(
        self, kwargs: dict[str, Any], dropped: list[str]
    ) -> ModelResponse:
        """Call the provider, shedding sampling parameters it rejects.

        Terminates because each iteration removes a key from ``kwargs`` and
        only parameters still present can be identified as the culprit — so
        a provider that keeps blaming a parameter we no longer send raises
        rather than looping.
        """
        while True:
            try:
                return completion(**kwargs)
            except BadRequestError as error:
                param = _rejected_sampling_param(error, kwargs)
                if param is None:
                    raise
                kwargs.pop(param)
                self._unsupported_params.add(param)
                dropped.append(param)

    def _retry_enabled_for(self, error: BaseException) -> bool:
        """Apply per-category retry switches from config."""
        if isinstance(error, RateLimitError):
            return self.config.retry_on_rate_limit
        if isinstance(error, Timeout):
            return self.config.retry_on_timeout
        return True

    @staticmethod
    def estimate_cost(response: ModelResponse) -> float:
        return completion_cost(response)

    def _completion_kwargs(self, prompt: str) -> dict[str, Any]:
        """Kwargs as they will actually be sent, minus known-bad parameters."""
        requested = self._requested_kwargs(prompt)
        return {k: v for k, v in requested.items() if k not in self._unsupported_params}

    def _requested_kwargs(self, prompt: str) -> dict[str, Any]:
        """Kwargs as configured, before any learned parameter is stripped."""
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {
            "model": self.config.name,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "timeout": self.config.request_timeout,
        }
        # Sampling parameters are opt-in: sending them by default breaks
        # every current frontier model and never bought reproducibility.
        # See ModelConfig.temperature.
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if self.config.top_p is not None:
            kwargs["top_p"] = self.config.top_p
        return kwargs


def _rejected_sampling_param(error: BadRequestError, kwargs: dict[str, Any]) -> str | None:
    """Name the sampling parameter a bad request is complaining about.

    Providers word this differently and change the wording between model
    generations -- "is deprecated", "Extra inputs are not permitted",
    "Unsupported value", "is not supported with this model". Matching any
    one phrase is what made the previous implementation brittle, so we key
    on the parameter name instead: if a rejection names a droppable
    parameter we actually sent, that parameter is the thing to drop.

    Returns None for every other bad request, which then fails fast.
    """
    message = str(error)
    for param in _DROPPABLE_SAMPLING_PARAMS:
        if param in kwargs and re.search(rf"\b{re.escape(param)}\b", message):
            return param
    return None


def _extract_usage(response: Any) -> dict[str, int | None]:
    """Read token usage defensively; providers may omit any field."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    def _read(field: str) -> int | None:
        if usage is None:
            return None
        value = getattr(usage, field, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(field)
        return int(value) if isinstance(value, (int, float)) else None

    return {
        "prompt_tokens": _read("prompt_tokens"),
        "completion_tokens": _read("completion_tokens"),
        "total_tokens": _read("total_tokens"),
    }


def _estimate_cost_safe(response: Any) -> float | None:
    """Best-effort cost estimate via LiteLLM; None when unpriceable.

    Cost is an estimate, not billing truth: unknown models, custom
    endpoints, and local providers have no pricing data, and estimation
    failures must never abort a benchmark run.
    """
    try:
        cost = completion_cost(response)
    except Exception:  # noqa: BLE001 -- best-effort estimate over arbitrary providers
        return None
    return float(cost) if isinstance(cost, (int, float)) else None
