"""Tests for model-call retry, backoff, and rate-limit handling."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from litellm.exceptions import BadRequestError, RateLimitError, Timeout

import wp_bench.models as models_module
from wp_bench.config import ModelConfig
from wp_bench.models import ModelInterface


def _ok_response(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message={"content": text})],
        id="resp-123",
    )


def _rate_limit() -> RateLimitError:
    return RateLimitError(message="rate limited", llm_provider="openai", model="gpt-4o-mini")


def _timeout() -> Timeout:
    return Timeout(message="timed out", model="gpt-4o-mini", llm_provider="openai")


def _fast_config(**overrides: object) -> ModelConfig:
    """Retry config with sub-second backoff so tests stay fast."""
    defaults: dict = {
        "name": "gpt-4o-mini",
        "max_retries": 3,
        "retry_min_seconds": 0.01,
        "retry_max_seconds": 0.02,
    }
    defaults.update(overrides)
    return ModelConfig.model_validate(defaults)


def test_generate_retries_transient_error_then_succeeds(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) <= 2:
            raise _rate_limit()
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    model = ModelInterface(_fast_config())

    generation = model.generate_with_metadata("hello")

    assert generation.text == "ok"
    assert generation.retry_count == 2
    assert generation.provider_response_id == "resp-123"
    assert generation.latency_ms >= 0
    assert len(calls) == 3


def test_generate_text_wrapper_still_works(monkeypatch) -> None:
    monkeypatch.setattr(models_module, "completion", lambda **kwargs: _ok_response("text"))
    model = ModelInterface(_fast_config())

    assert model.generate("hello") == "text"


def test_retry_exhaustion_raises_original_error(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise _rate_limit()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    model = ModelInterface(_fast_config(max_retries=2))

    with pytest.raises(RateLimitError):
        model.generate_with_metadata("hello")

    assert len(calls) == 3  # first attempt + 2 retries


def test_non_retryable_bad_request_fails_fast(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise BadRequestError(
            message="invalid model name",
            model="gpt-4o-mini",
            llm_provider="openai",
        )

    monkeypatch.setattr(models_module, "completion", fake_completion)
    model = ModelInterface(_fast_config())

    with pytest.raises(BadRequestError):
        model.generate_with_metadata("hello")

    assert len(calls) == 1


def test_rate_limit_retry_can_be_disabled(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise _rate_limit()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    model = ModelInterface(_fast_config(retry_on_rate_limit=False))

    with pytest.raises(RateLimitError):
        model.generate_with_metadata("hello")

    assert len(calls) == 1


def test_timeout_retry_can_be_disabled(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise _timeout()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    model = ModelInterface(_fast_config(retry_on_timeout=False))

    with pytest.raises(Timeout):
        model.generate_with_metadata("hello")

    assert len(calls) == 1


def test_deprecated_temperature_fallback_is_flagged_not_counted(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise BadRequestError(
                message="`temperature` is deprecated for this model.",
                model="gpt-4o-mini",
                llm_provider="openai",
            )
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    # temperature is opt-in now; this fallback only applies when it is set.
    model = ModelInterface(_fast_config(temperature=0.0))

    generation = model.generate_with_metadata("hello")

    assert generation.text == "ok"
    assert generation.temperature_fallback is True
    assert generation.retry_count == 0  # fallback is not a transient retry
    assert "temperature" not in calls[1]


def test_retry_config_from_values() -> None:
    config = ModelConfig(
        max_retries=5,
        retry_min_seconds=2.0,
        retry_max_seconds=60.0,
        retry_on_rate_limit=False,
    )
    assert config.max_retries == 5
    assert config.retry_on_rate_limit is False


def test_retry_config_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        ModelConfig(retry_min_seconds=10.0, retry_max_seconds=1.0)
    with pytest.raises(ValueError, match="must be >= 0"):
        ModelConfig(max_retries=-1)
