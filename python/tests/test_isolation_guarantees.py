"""Tests for issue #39: a run must never claim isolation it did not perform.

``reset_per_test`` is the foundation of reproducible scores, so a reset that
silently does nothing (or silently fails) is worse than no reset at all: the
results file still stamps ``runtime_isolation: reset_per_test``.
"""
from __future__ import annotations

from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import EnvironmentSetupTimeout, WordPressEnvironment


def _docker_env(monkeypatch: pytest.MonkeyPatch, result: tuple[str, str, int, bool]):
    """A docker-grader environment whose commands return a canned result."""
    environment = WordPressEnvironment(GraderConfig(kind="docker"))
    calls: list[list[str]] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append(command)
        return result

    monkeypatch.setattr(environment, "_exec", fake_exec)
    # Stand in for what setup() would have captured; these tests exercise
    # reset() in isolation.
    environment._baseline = "-- MariaDB dump\n"
    return environment, calls


def test_cli_grader_refuses_to_pretend_it_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cli grader has no reset, and used to return silently: the run then
    graded every test against state left by the previous one while recording
    that it had not."""
    environment = WordPressEnvironment(GraderConfig(kind="cli"))

    with pytest.raises(RuntimeError, match="no reset implementation"):
        environment.reset()


def test_docker_reset_raises_when_a_step_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed reset leaves the next test on dirty or uninstalled WordPress,
    and its assertion failures get blamed on the model."""
    environment, _ = _docker_env(monkeypatch, ("", "MySQL server has gone away", 1, False))

    with pytest.raises(RuntimeError, match="exit code 1"):
        environment.reset()


def test_docker_reset_surfaces_the_failing_command_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _ = _docker_env(monkeypatch, ("", "connection refused", 2, False))

    with pytest.raises(RuntimeError, match="connection refused"):
        environment.reset()


def test_docker_reset_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _ = _docker_env(monkeypatch, ("", "", 0, True))

    with pytest.raises(EnvironmentSetupTimeout, match="Timed out resetting"):
        environment.reset()


def test_docker_reset_restores_a_baseline_after_dropping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """db reset drops every table, so a restore must follow to get back to a
    deterministic just-installed state.

    The restore replays the baseline dump captured at setup rather than
    reinstalling; see test_template_reset.py for the mechanism.
    """
    environment, calls = _docker_env(monkeypatch, ("ok", "", 0, False))

    environment.reset()

    script = calls[0][2]
    assert "wp db reset --yes" in script
    assert "wp db import -" in script
