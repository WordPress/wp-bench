"""Tests for template-baseline reset.

``reset_per_test`` used to rebuild WordPress from scratch before every
execution test: ``wp db reset`` followed by a full ``wp core install``, as two
separate round trips into the runtime. Reinstalling is the slow way to reach a
state the harness already knows: it is the same state every time. So the run
captures that state once at setup and restores the dump per test instead.

The guarantee these tests protect is unchanged from issue #39 — a run must
never claim isolation it did not perform — so a restore that silently no-ops
or silently fails is still worse than no reset at all.
"""
from __future__ import annotations

from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import (
    BASELINE_DUMP_PATH,
    EnvironmentSetupTimeout,
    WordPressEnvironment,
)


def _env(config: GraderConfig, result: tuple[str, str, int, bool]):
    """An environment whose runtime commands return a canned result."""
    environment = WordPressEnvironment(config)
    calls: list[list[str]] = []

    def fake_exec(command: list[str], **kwargs: Any) -> tuple[str, str, int, bool]:
        calls.append(command)
        return result

    environment._exec = fake_exec  # type: ignore[method-assign]
    return environment, calls


def _script(call: list[str]) -> str:
    """The shell script body from an ``sh -c`` invocation."""
    assert call[:2] == ["sh", "-c"], f"expected an sh -c invocation, got {call!r}"
    return call[2]


# Restore ---------------------------------------------------------------


def test_reset_restores_the_baseline_instead_of_reinstalling() -> None:
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))

    environment.reset()

    script = _script(calls[0])
    assert "wp db reset --yes" in script
    assert f"wp db import {BASELINE_DUMP_PATH}" in script
    assert "wp core install" not in script


def test_reset_is_a_single_round_trip() -> None:
    """Two execs per test cost more in round-trip latency than the reset
    itself; the drop and the restore travel together."""
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))

    environment.reset()

    assert len(calls) == 1


def test_reset_drops_before_it_restores() -> None:
    """Importing over a live schema merges rather than replaces, so state from
    the previous test would survive into the next one."""
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))

    environment.reset()

    script = _script(calls[0])
    assert script.index("wp db reset") < script.index("wp db import")


def test_reset_chains_so_a_failed_drop_aborts_the_restore() -> None:
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))

    environment.reset()

    assert "&&" in _script(calls[0])


def test_wp_env_reset_also_uses_the_template_restore(tmp_path) -> None:
    """The wp-env path is the one every local run actually takes."""
    config = GraderConfig(kind="docker", wp_env_dir=tmp_path)
    environment, calls = _env(config, ("ok", "", 0, False))

    environment.reset()

    assert len(calls) == 1
    assert f"wp db import {BASELINE_DUMP_PATH}" in _script(calls[0])


# Capture ---------------------------------------------------------------


def _captured_script(monkeypatch: pytest.MonkeyPatch, config: GraderConfig) -> str:
    environment, calls = _env(config, ("ok", "", 0, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    monkeypatch.setattr(environment, "_run_wp_env", lambda command: None)

    environment.setup()

    assert len(calls) == 1, f"expected one capture exec, got {calls!r}"
    return _script(calls[0])


def test_setup_captures_the_baseline_from_a_clean_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dump must come from exactly the state the old per-test reset
    produced, or restoring it grades against a different WordPress."""
    script = _captured_script(monkeypatch, GraderConfig(kind="docker"))

    assert "wp db reset --yes" in script
    assert "wp core install" in script
    assert f"wp db export {BASELINE_DUMP_PATH}" in script


def test_capture_installs_before_it_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _captured_script(monkeypatch, GraderConfig(kind="docker"))

    assert script.index("wp core install") < script.index("wp db export")


def test_capture_uses_the_configured_site_url(monkeypatch: pytest.MonkeyPatch) -> None:
    config = GraderConfig(kind="docker", base_url="http://wp-bench.example:9999")
    script = _captured_script(monkeypatch, config)

    assert "--url=http://wp-bench.example:9999" in script


def test_wp_env_setup_captures_a_baseline_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    config = GraderConfig(kind="docker", wp_env_dir=tmp_path)
    script = _captured_script(monkeypatch, config)

    assert f"wp db export {BASELINE_DUMP_PATH}" in script


def test_capture_and_restore_agree_on_the_dump_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore reading a path nothing wrote leaves WordPress uninstalled
    while the results file still stamps reset_per_test."""
    capture = _captured_script(monkeypatch, GraderConfig(kind="docker"))
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))
    environment.reset()

    assert BASELINE_DUMP_PATH in capture
    assert BASELINE_DUMP_PATH in _script(calls[0])


# Failure handling ------------------------------------------------------


def test_failed_capture_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every later reset restores this dump, so a capture that failed would
    break every test in the run, not just one."""
    environment, _ = _env(GraderConfig(kind="docker"), ("", "no space left on device", 1, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    with pytest.raises(RuntimeError, match="no space left on device"):
        environment.setup()


def test_failed_capture_names_the_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _ = _env(GraderConfig(kind="docker"), ("", "boom", 1, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    with pytest.raises(RuntimeError, match="baseline"):
        environment.setup()


def test_capture_timeout_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _ = _env(GraderConfig(kind="docker"), ("", "", 0, True))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    with pytest.raises(EnvironmentSetupTimeout):
        environment.setup()


def test_failed_restore_raises() -> None:
    environment, _ = _env(GraderConfig(kind="docker"), ("", "MySQL server has gone away", 1, False))

    with pytest.raises(RuntimeError, match="MySQL server has gone away"):
        environment.reset()


def test_restore_timeout_raises() -> None:
    environment, _ = _env(GraderConfig(kind="docker"), ("", "", 0, True))

    with pytest.raises(EnvironmentSetupTimeout, match="Timed out resetting"):
        environment.reset()


def test_cli_grader_still_refuses_to_pretend_it_reset() -> None:
    """A cli grader has no runtime to reset; it must not silently succeed."""
    environment = WordPressEnvironment(GraderConfig(kind="cli"))

    with pytest.raises(RuntimeError, match="no reset implementation"):
        environment.reset()


def test_cli_grader_captures_no_baseline() -> None:
    """setup() must stay a no-op for a grader that has no runtime to install."""
    environment, calls = _env(GraderConfig(kind="cli"), ("ok", "", 0, False))

    environment.setup()

    assert calls == []
