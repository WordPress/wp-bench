"""Tests for the stdin payload transport to the runtime verifier."""
from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import WordPressEnvironment


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub subprocess.run and capture how it was invoked."""
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen["command"] = command
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout='{"success": true}', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_execute_code_sends_payload_via_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_run(monkeypatch)
    environment = WordPressEnvironment(GraderConfig(kind="cli"))

    environment.execute_code("function demo() {}", {"static_checks": {}, "runtime_checks": {}})

    payload = json.loads(seen["input"])
    assert payload["code"] == "function demo() {}"
    assert payload["payload_version"] == "1.0"
    # Payload must not appear in argv.
    assert all("demo" not in part for part in seen["command"])


def test_command_stays_small_for_large_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload far beyond argv limits must not grow the command line."""
    seen = _capture_run(monkeypatch)
    environment = WordPressEnvironment(GraderConfig(kind="cli"))
    huge_code = "x" * 2_000_000  # ~2MB, well past typical ARG_MAX

    environment.execute_code(huge_code, {"static_checks": {}, "runtime_checks": {}})

    command_size = sum(len(part) for part in seen["command"])
    assert command_size < 1024
    assert len(seen["input"]) > 2_000_000


def test_payload_is_plain_json_not_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_run(monkeypatch)
    environment = WordPressEnvironment(GraderConfig(kind="cli"))

    environment.execute_code("code", {"static_checks": {"a": 1}, "runtime_checks": {}})

    # Directly parseable as JSON: no base64 layer.
    payload = json.loads(seen["input"])
    assert payload["static_checks"] == {"a": 1}


def test_docker_path_pipes_stdin_through_docker_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_run(monkeypatch)
    environment = WordPressEnvironment(GraderConfig(kind="docker"))

    environment.execute_code("code", {})

    assert seen["command"][:3] == ["docker", "exec", "-i"]
    assert json.loads(seen["input"])["code"] == "code"
