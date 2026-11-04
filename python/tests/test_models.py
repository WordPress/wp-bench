from __future__ import annotations

from types import SimpleNamespace

import pytest
from litellm.exceptions import BadRequestError, UnsupportedParamsError

import wp_bench.models as models_module
from wp_bench.config import ModelConfig
from wp_bench.models import ModelInterface


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


def test_generate_drops_temperature_litellm_rejects_and_remembers_it(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if "temperature" in kwargs:
            raise UnsupportedParamsError(
                message="claude-opus-5-5 does not support temperature=0. Only temperature=1 is supported.",
                model="anthropic/claude-opus-5-5",
                llm_provider="anthropic",
            )
        return SimpleNamespace(choices=[SimpleNamespace(message={"content": "ok"})])

    monkeypatch.setattr(models_module, "completion", fake_completion)

    model = ModelInterface(ModelConfig(name="anthropic/claude-opus-5-5"))
    first = model.generate_with_metadata("hello")
    second = model.generate_with_metadata("again")

    assert first.text == "ok" and second.text == "ok"
    assert first.temperature_fallback and second.temperature_fallback
    assert ["temperature" in call for call in calls] == [True, False, False]


def test_generate_drops_temperature_for_openai_wording(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if "temperature" in kwargs:
            raise UnsupportedParamsError(
                message="gpt-5 models (including gpt-5-codex) don't support temperature=0.0. Only temperature=1 is supported.",
                model="gpt-5",
                llm_provider="openai",
            )
        return SimpleNamespace(choices=[SimpleNamespace(message={"content": "ok"})])

    monkeypatch.setattr(models_module, "completion", fake_completion)

    result = ModelInterface(ModelConfig(name="gpt-5")).generate_with_metadata("hello")

    assert result.text == "ok" and result.temperature_fallback
    assert "temperature" not in calls[-1]


def test_generate_reraises_unrelated_bad_request(monkeypatch) -> None:
    def fake_completion(**kwargs):
        raise BadRequestError(message="prompt is too long", model="m", llm_provider="anthropic")

    monkeypatch.setattr(models_module, "completion", fake_completion)

    model = ModelInterface(ModelConfig(name="anthropic/claude-opus-5-5"))
    with pytest.raises(BadRequestError):
        model.generate("hello")
    assert "temperature" in model._completion_kwargs("x")


def test_generate_normalizes_none_content_to_empty_string(monkeypatch) -> None:
    def fake_completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message={"content": None})])

    monkeypatch.setattr(models_module, "completion", fake_completion)

    model = ModelInterface(ModelConfig(name="gemini/gemini-2.5-pro"))
    result = model.generate_with_metadata("hello")

    assert result.text == ""


def test_completion_kwargs_omit_system_message_by_default() -> None:
    model = ModelInterface(ModelConfig(name="gpt-4o-mini"))
    messages = model._completion_kwargs("hello")["messages"]
    assert messages == [{"role": "user", "content": "hello"}]


def test_completion_kwargs_prepend_system_prompt_when_set() -> None:
    model = ModelInterface(ModelConfig(name="gpt-4o-mini"), system_prompt="Skill content.")
    messages = model._completion_kwargs("hello")["messages"]
    assert messages == [
        {"role": "system", "content": "Skill content."},
        {"role": "user", "content": "hello"},
    ]


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


def test_local_endpoint_routes_without_model_prefix_or_cloud_credentials(monkeypatch, capsys) -> None:
    """Exercise real LiteLLM routing against a local OpenAI-compatible server."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from wp_bench.records import _model_info

    received: list[tuple[str, str | None, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, self.headers.get('Authorization'), body))
            response = json.dumps({
                'id': 'local-test', 'object': 'chat.completion', 'created': 1,
                'model': body['model'],
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    monkeypatch.setenv('OPENAI_API_KEY', 'cloud-secret-must-not-be-sent')
    monkeypatch.setenv('OPENAI_API_BASE', 'http://127.0.0.1:1/v1')
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            config = ModelConfig(kind='openai-compatible', name='qwen3.6-27b-mtp',
                                 api_base=f'http://127.0.0.1:{server.server_port}/v1',
                                 max_retries=0, request_timeout=5)
            assert ModelInterface(config).generate('hello') == 'ok'
        finally:
            server.shutdown()
            worker.join()
    assert received[0][0] == '/v1/chat/completions'
    assert received[0][1] == 'Bearer not-needed'
    assert received[0][2]['model'] == 'qwen3.6-27b-mtp'
    info = _model_info(config)
    assert info is not None and info['name'] == 'qwen3.6-27b-mtp'
    assert info['api_base'] == config.api_base
    assert 'cloud-secret' not in config.model_dump_json()
    assert 'Provider List:' not in capsys.readouterr().out


def test_explicit_api_key_env_is_resolved_without_serializing_secret(monkeypatch) -> None:
    monkeypatch.setenv('LM_STUDIO_TOKEN', 'private-local-token')
    config = ModelConfig(kind='openai-compatible', name='local-model',
                         api_base='http://localhost:1234/v1', api_key_env='LM_STUDIO_TOKEN')
    assert ModelInterface(config)._completion_kwargs('hello')['api_key'] == 'private-local-token'
    assert 'private-local-token' not in config.model_dump_json()
    monkeypatch.delenv('LM_STUDIO_TOKEN')
    with pytest.raises(ValueError, match='LM_STUDIO_TOKEN'):
        ModelInterface(config).generate('hello')


def test_provider_error_is_raised_without_advisory_banner(capsys) -> None:
    model = ModelInterface(ModelConfig(name="wp-bench-unknown-model", max_retries=0))
    with pytest.raises(BadRequestError):
        model.generate("hello")
    assert "Provider List:" not in capsys.readouterr().out
