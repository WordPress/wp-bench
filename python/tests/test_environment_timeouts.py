"""Tests for hard timeout enforcement across environment subprocess paths."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import (
    EnvironmentSetupTimeout,
    ExecutionResult,
    WordPressEnvironment,
)


def _raise_timeout(*args: Any, **kwargs: Any) -> None:
    raise subprocess.TimeoutExpired(
        cmd=args[0] if args else kwargs.get("args", ["cmd"]),
        timeout=kwargs.get("timeout", 1),
        output=b"partial stdout",
        stderr=b"partial stderr",
    )


def test_exec_passes_timeout_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every _exec path forwards grader.timeout_seconds to subprocess.run."""
    seen: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    environment = WordPressEnvironment(GraderConfig(kind="cli", timeout_seconds=42))

    environment._exec(["wp", "cli", "version"])

    assert seen["timeout"] == 42


def test_exec_times_out_returns_timed_out_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung subprocess surfaces as timed_out=True with partial output."""
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    environment = WordPressEnvironment(GraderConfig(kind="cli", timeout_seconds=1))

    stdout, stderr, returncode, timed_out = environment._exec(["wp", "eval-file", "x"])

    assert timed_out is True
    assert returncode == -1
    assert stdout == "partial stdout"
    assert stderr == "partial stderr"


def test_execute_code_timeout_returns_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime timeout becomes a scored per-test failure, not a crash."""
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    environment = WordPressEnvironment(GraderConfig(kind="cli", timeout_seconds=7))

    result = environment.execute_code("while (true) {}", {"static_checks": {}, "runtime_checks": {}})

    assert isinstance(result, ExecutionResult)
    assert result.success is False
    assert result.timed_out is True
    assert result.raw["timeout"] is True
    assert result.raw["success"] is False
    assert result.raw["runtime"]["score"] == 0.0
    assertions = result.raw["runtime"]["details"]["assertions"]
    assert assertions[0]["type"] == "timeout"
    assert assertions[0]["timeout_seconds"] == 7


def test_execute_code_timeout_preserves_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    environment = WordPressEnvironment(GraderConfig(kind="cli"))

    result = environment.execute_code("code", {})

    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr"


def test_setup_timeout_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """wp-env setup timeout is a harness failure with actionable message."""
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    environment = WordPressEnvironment(
        GraderConfig(kind="docker", wp_env_dir=Path("runtime"), setup_timeout_seconds=5)
    )

    with pytest.raises(EnvironmentSetupTimeout, match="Timed out running npx wp-env start"):
        environment.setup()


def test_start_container_timeout_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    environment = WordPressEnvironment(GraderConfig(kind="docker", timeout_seconds=5))

    with pytest.raises(EnvironmentSetupTimeout):
        environment._start_container()


def test_container_exists_timeout_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    environment = WordPressEnvironment(GraderConfig(kind="docker"))

    with pytest.raises(EnvironmentSetupTimeout, match="Docker daemon"):
        environment._container_exists()


def test_run_wp_env_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nonzero wp-env exits keep failing loudly (previous check=True behavior)."""

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=args[0], returncode=3, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    environment = WordPressEnvironment(
        GraderConfig(kind="docker", wp_env_dir=Path("runtime"))
    )

    with pytest.raises(RuntimeError, match="exit code 3"):
        environment._run_wp_env(["npx", "wp-env", "start"])
