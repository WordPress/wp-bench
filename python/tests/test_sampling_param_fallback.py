"""Sampling parameters: opt-in, and survivable when a provider rejects one.

No sampling parameter is sent unless a config asks for it, because every
current frontier model rejects them. When one *is* asked for and rejected,
the harness drops it and re-sends rather than aborting the suite -- without
matching on any single vendor's wording, which is what made the previous
implementation brittle.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from litellm.exceptions import BadRequestError

import wp_bench.models as models_module
from wp_bench.config import ModelConfig
from wp_bench.models import ModelInterface


def _ok_response():
    return SimpleNamespace(choices=[SimpleNamespace(message={"content": "ok"})])


def _bad_request(message: str) -> BadRequestError:
    return BadRequestError(
        message=message,
        model="test-model",
        llm_provider="test-provider",
    )


def _fast_config(**overrides: object) -> ModelConfig:
    """Sub-second backoff so tests stay fast; no sampling params by default."""
    defaults: dict = {
        "name": "test-model",
        "max_retries": 0,
        "retry_min_seconds": 0.001,
        "retry_max_seconds": 0.002,
    }
    defaults.update(overrides)
    return ModelConfig.model_validate(defaults)


def test_no_sampling_parameters_are_sent_by_default() -> None:
    """The default request cannot provoke the rejection in the first place."""
    kwargs = ModelInterface(_fast_config())._completion_kwargs("hello")

    assert "top_p" not in kwargs  # nor an explicit null, which differs from absent
    assert "top_k" not in kwargs
    assert set(kwargs) == {"model", "messages", "max_tokens", "timeout"}


def test_temperature_is_not_a_config_option() -> None:
    """Removed outright: every current frontier model rejects it, and a fixed
    temperature never bought the reproducibility it appeared to."""
    assert not hasattr(ModelConfig(), "temperature")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        ModelConfig(temperature=0.0)  # type: ignore[call-arg]


def test_top_p_is_sent_when_explicitly_configured() -> None:
    kwargs = ModelInterface(_fast_config(top_p=0.9))._completion_kwargs("hello")

    assert kwargs["top_p"] == 0.9


# Wordings observed across providers and generations for one condition:
# this model will not accept this sampling parameter.
REJECTION_WORDINGS = [
    pytest.param("`top_p` is deprecated for this model.", id="deprecated"),
    pytest.param("Exception - top_p: Extra inputs are not permitted", id="removed"),
    pytest.param(
        "Unsupported value: 'top_p' does not support 0.9 with this model.",
        id="unsupported-value",
    ),
    pytest.param('Invalid request: parameter "top_p" is not supported.', id="not-supported"),
]


@pytest.mark.parametrize("message", REJECTION_WORDINGS)
def test_drops_rejected_param_regardless_of_provider_wording(monkeypatch, message: str) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _bad_request(message)
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(top_p=0.9)).generate_with_metadata("hello")

    assert generation.text == "ok"
    assert len(calls) == 2
    assert calls[0]["top_p"] == 0.9
    assert "top_p" not in calls[1]
    assert generation.dropped_params == ("top_p",)


def test_unrelated_bad_request_is_not_swallowed(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise _bad_request("This model's maximum context length is 8192 tokens.")

    monkeypatch.setattr(models_module, "completion", fake_completion)

    with pytest.raises(BadRequestError, match="maximum context length"):
        ModelInterface(_fast_config(top_p=0.9)).generate_with_metadata("hello")

    assert len(calls) == 1


def test_rejection_naming_an_already_dropped_parameter_reraises(monkeypatch) -> None:
    """A provider that keeps blaming a parameter we no longer send must not loop."""
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise _bad_request("top_p is not supported with this model")

    monkeypatch.setattr(models_module, "completion", fake_completion)

    with pytest.raises(BadRequestError):
        ModelInterface(_fast_config(top_p=0.9)).generate_with_metadata("hello")

    assert len(calls) == 2
    assert "top_p" in calls[0]
    assert "top_p" not in calls[1]


def test_unsupported_parameter_is_remembered_for_later_calls(monkeypatch) -> None:
    """The 400 is paid once per model, not once per test in a suite."""
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if "top_p" in kwargs:
            raise _bad_request("top_p is not supported with this model")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    model = ModelInterface(_fast_config(top_p=0.9))

    first = model.generate_with_metadata("hello")
    second = model.generate_with_metadata("again")

    assert first.text == "ok"
    assert second.text == "ok"
    # Call 1 fails, call 2 succeeds, call 3 is the second generate() -- which
    # must not repeat the known-bad parameter.
    assert len(calls) == 3
    assert "top_p" not in calls[2]
    assert second.dropped_params == ("top_p",)


def test_fallback_is_not_counted_as_a_transient_retry(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _bad_request("top_p is not supported with this model")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(top_p=0.9)).generate_with_metadata("hello")

    assert generation.retry_count == 0


def test_dropped_params_are_recorded_in_call_metadata(monkeypatch) -> None:
    """Auditable in the result record: usage_dict() stays tokens/cost/latency."""
    from wp_bench.core import _model_call_info

    def fake_completion(**kwargs):
        if "top_p" in kwargs:
            raise _bad_request("top_p is not supported with this model")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(top_p=0.9)).generate_with_metadata("hello")

    assert _model_call_info(generation)["dropped_params"] == ["top_p"]
    assert "dropped_params" not in generation.usage_dict()
