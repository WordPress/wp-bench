"""Model interface leveraging LiteLLM providers.

Model calls are wrapped in a bounded, configurable retry policy
(tenacity, exponential backoff with jitter). Only transient provider
failures are retried — rate limits, timeouts, connection drops, and
5xx/internal errors. Deterministic request errors (bad model name, bad
API key, context overflow, malformed request) fail fast so systemic
problems are not hidden behind retries.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

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
    raw_response: Optional[ModelResponse]
    retry_count: int
    latency_ms: float
    provider_response_id: Optional[str]
    temperature_fallback: bool = False
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None

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

    def __init__(self, config: ModelConfig):
        self.config = config

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
        kwargs = self._completion_kwargs(prompt)
        started = time.perf_counter()
        attempt_count = 0
        temperature_fallback = False

        def _should_retry(error: BaseException) -> bool:
            if not self._retry_enabled_for(error):
                return False
            return isinstance(error, _TRANSIENT_ERRORS)

        def _before(retry_state: RetryCallState) -> None:
            nonlocal attempt_count
            attempt_count = retry_state.attempt_number

        response: Optional[ModelResponse] = None
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
                try:
                    response = completion(**kwargs)
                except BadRequestError as error:
                    # Deterministic error, except the known deprecated-
                    # temperature case which is fixable by dropping the
                    # parameter. That fallback is intentional behavior,
                    # not a retry, and is flagged separately.
                    if not _is_deprecated_temperature_error(error) or "temperature" not in kwargs:
                        raise
                    kwargs.pop("temperature")
                    temperature_fallback = True
                    response = completion(**kwargs)

        assert response is not None  # Retrying(reraise=True) raises otherwise
        latency_ms = (time.perf_counter() - started) * 1000
        choice = response.choices[0]
        usage = _extract_usage(response)
        return ModelGeneration(
            text=choice.message["content"],  # type: ignore[union-attr, index]
            raw_response=response,
            retry_count=attempt_count - 1,
            latency_ms=latency_ms,
            provider_response_id=getattr(response, "id", None),
            temperature_fallback=temperature_fallback,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            cost_usd=_estimate_cost_safe(response),
        )

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
        kwargs: dict[str, Any] = {
            "model": self.config.name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.config.max_tokens,
            "top_p": self.config.top_p,
            "timeout": self.config.request_timeout,
        }
        kwargs["temperature"] = self.config.temperature
        return kwargs


def _is_deprecated_temperature_error(error: BadRequestError) -> bool:
    return "`temperature` is deprecated" in str(error)


def _extract_usage(response: Any) -> dict[str, Optional[int]]:
    """Read token usage defensively; providers may omit any field."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    def _read(field: str) -> Optional[int]:
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


def _estimate_cost_safe(response: Any) -> Optional[float]:
    """Best-effort cost estimate via LiteLLM; None when unpriceable.

    Cost is an estimate, not billing truth: unknown models, custom
    endpoints, and local providers have no pricing data, and estimation
    failures must never abort a benchmark run.
    """
    try:
        cost = completion_cost(response)
    except Exception:
        return None
    return float(cost) if isinstance(cost, (int, float)) else None
