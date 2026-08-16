"""Durability of the unsupported-sampling-parameter fallback.

Providers retire sampling parameters (``temperature``, ``top_p``, ``top_k``)
on a per-model basis and describe the rejection in whatever prose they like.
The harness must recover by dropping the offending parameter rather than
aborting a suite, without matching on any single vendor's wording.
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
    """Sub-second backoff so tests stay fast; no temperature unless asked."""
    defaults: dict = {
        "name": "test-model",
        "max_retries": 0,
        "retry_min_seconds": 0.001,
        "retry_max_seconds": 0.002,
    }
    defaults.update(overrides)
    return ModelConfig.model_validate(defaults)


# Wordings observed across providers and generations for the same underlying
# condition: this model will not accept this sampling parameter.
REJECTION_WORDINGS = [
    pytest.param("`temperature` is deprecated for this model.", id="anthropic-deprecated"),
    pytest.param(
        "AnthropicException - temperature: Extra inputs are not permitted",
        id="anthropic-removed",
    ),
    pytest.param(
        "Unsupported value: 'temperature' does not support 0.0 with this model. "
        "Only the default (1) value is supported.",
        id="openai-unsupported-value",
    ),
    pytest.param(
        'Invalid request: parameter "temperature" is not supported.',
        id="generic-not-supported",
    ),
]


def test_temperature_is_not_sent_by_default() -> None:
    """The default request carries no sampling parameter that a current
    frontier model would reject -- no fallback round trip required."""
    kwargs = ModelInterface(_fast_config())._completion_kwargs("hello")

    assert "temperature" not in kwargs
    # Nor an explicit null top_p, which is not the same thing as absent.
    assert "top_p" not in kwargs
    assert ModelConfig().temperature is None


def test_temperature_is_sent_when_explicitly_configured() -> None:
    """Local and older backends can still ask for it."""
    kwargs = ModelInterface(_fast_config(temperature=0.2))._completion_kwargs("hello")

    assert kwargs["temperature"] == 0.2


@pytest.mark.parametrize("message", REJECTION_WORDINGS)
def test_drops_temperature_regardless_of_provider_wording(monkeypatch, message: str) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _bad_request(message)
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(temperature=0.0)).generate_with_metadata("hello")

    assert generation.text == "ok"
    assert len(calls) == 2
    assert calls[0]["temperature"] == 0.0
    assert "temperature" not in calls[1]
    assert generation.temperature_fallback is True
    assert generation.dropped_params == ("temperature",)


def test_drops_top_p_when_that_is_the_rejected_parameter(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _bad_request("Unsupported parameter: 'top_p' is not supported with this model.")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(top_p=0.9)).generate_with_metadata("hello")

    assert generation.text == "ok"
    assert calls[0]["top_p"] == 0.9
    assert "top_p" not in calls[1]
    # top_p is not temperature: the legacy flag must not be raised for it.
    assert generation.temperature_fallback is False
    assert generation.dropped_params == ("top_p",)


def test_drops_several_parameters_across_successive_rejections(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if "temperature" in kwargs:
            raise _bad_request("temperature is not supported with this model")
        if "top_p" in kwargs:
            raise _bad_request("top_p is not supported with this model")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(top_p=0.9, temperature=0.0)).generate_with_metadata("hello")

    assert generation.text == "ok"
    assert len(calls) == 3
    assert set(generation.dropped_params) == {"temperature", "top_p"}


def test_unrelated_bad_request_is_not_swallowed(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise _bad_request("This model's maximum context length is 8192 tokens.")

    monkeypatch.setattr(models_module, "completion", fake_completion)

    with pytest.raises(BadRequestError, match="maximum context length"):
        ModelInterface(_fast_config()).generate_with_metadata("hello")

    assert len(calls) == 1


def test_rejection_naming_an_already_dropped_parameter_reraises(monkeypatch) -> None:
    """A provider that keeps blaming a parameter we no longer send must not loop."""
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        raise _bad_request("temperature is not supported with this model")

    monkeypatch.setattr(models_module, "completion", fake_completion)

    with pytest.raises(BadRequestError):
        ModelInterface(_fast_config(temperature=0.0)).generate_with_metadata("hello")

    # One call with temperature, one without, then give up.
    assert len(calls) == 2
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]


def test_unsupported_parameter_is_remembered_for_later_calls(monkeypatch) -> None:
    """The 400 is paid once per model, not once per test in a suite."""
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if "temperature" in kwargs:
            raise _bad_request("temperature is not supported with this model")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    model = ModelInterface(_fast_config(temperature=0.0))

    first = model.generate_with_metadata("hello")
    second = model.generate_with_metadata("again")

    assert first.text == "ok"
    assert second.text == "ok"
    # Call 1 fails, call 2 succeeds, call 3 is the second generate() — which
    # must not repeat the known-bad parameter.
    assert len(calls) == 3
    assert "temperature" not in calls[2]
    assert second.dropped_params == ("temperature",)


def test_fallback_is_not_counted_as_a_transient_retry(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _bad_request("temperature is not supported with this model")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(temperature=0.0)).generate_with_metadata("hello")

    assert generation.retry_count == 0


def test_dropped_params_are_recorded_in_call_metadata(monkeypatch) -> None:
    """Auditable in the result record: usage_dict() stays tokens/cost/latency."""
    from wp_bench.core import _model_call_info

    def fake_completion(**kwargs):
        if "temperature" in kwargs:
            raise _bad_request("temperature is not supported with this model")
        return _ok_response()

    monkeypatch.setattr(models_module, "completion", fake_completion)
    generation = ModelInterface(_fast_config(temperature=0.0)).generate_with_metadata("hello")

    assert _model_call_info(generation)["dropped_params"] == ["temperature"]
    assert "dropped_params" not in generation.usage_dict()
