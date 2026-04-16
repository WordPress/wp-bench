from __future__ import annotations

from types import SimpleNamespace

from litellm.exceptions import BadRequestError

from wp_bench.config import ModelConfig
from wp_bench.models import ModelInterface
import wp_bench.models as models_module


def test_generate_retries_without_temperature_on_deprecated_error(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise BadRequestError(
                message="AnthropicException - `temperature` is deprecated for this model.",
                model="anthropic/claude-opus-4-7",
                llm_provider="anthropic",
            )
        return SimpleNamespace(choices=[SimpleNamespace(message={"content": "ok"})])

    monkeypatch.setattr(models_module, "completion", fake_completion)

    model = ModelInterface(ModelConfig(name="anthropic/claude-opus-4-7"))
    result = model.generate("hello")

    assert result == "ok"
    assert len(calls) == 2
    assert calls[0]["temperature"] == 0.0
    assert "temperature" not in calls[1]


def test_generate_does_not_retry_other_bad_request_errors(monkeypatch) -> None:
    def fake_completion(**kwargs):
        raise BadRequestError(
            message="AnthropicException - some other invalid request.",
            model="anthropic/claude-opus-4-7",
            llm_provider="anthropic",
        )

    monkeypatch.setattr(models_module, "completion", fake_completion)

    model = ModelInterface(ModelConfig(name="anthropic/claude-opus-4-7"))

    try:
        model.generate("hello")
    except BadRequestError as error:
        assert "some other invalid request" in str(error)
    else:
        raise AssertionError("Expected BadRequestError to be raised")
