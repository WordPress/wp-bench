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
from wp_bench.environment import EnvironmentSetupTimeout, WordPressEnvironment

#: Stands in for the captured dump in tests that skip setup().
FAKE_BASELINE = "-- MariaDB dump\nINSERT INTO wp_options VALUES (1);\n"


def _env(
    config: GraderConfig,
    result: tuple[str, str, int, bool],
    *,
    baseline: str | None = FAKE_BASELINE,
):
    """An environment whose runtime commands return a canned result.

    ``baseline`` pre-seeds what setup() would have captured, so restore tests
    need not drive a full capture first. Pass None to test the unseeded path.
    """
    environment = WordPressEnvironment(config)
    calls: list[tuple[list[str], str | None]] = []

    def fake_exec(
        command: list[str], *, stdin: str | None = None, **kwargs: Any
    ) -> tuple[str, str, int, bool]:
        calls.append((command, stdin))
        return result

    environment._exec = fake_exec  # type: ignore[method-assign]
    environment._baseline = baseline
    return environment, calls


def _script(call: tuple[list[str], str | None]) -> str:
    """The shell script body from an ``sh -c`` invocation."""
    command = call[0]
    assert command[:2] == ["sh", "-c"], f"expected an sh -c invocation, got {command!r}"
    return command[2]


# Restore ---------------------------------------------------------------


def test_reset_restores_the_baseline_instead_of_reinstalling() -> None:
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))

    environment.reset()

    script = _script(calls[0])
    assert "wp db reset --yes" in script
    assert "wp db import -" in script
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


def test_reset_verifies_the_restore_actually_landed() -> None:
    """`wp db import` exits 0 on an empty or truncated dump — verified against
    a live runtime: the drop succeeds, nothing is imported, and WordPress is
    left uninstalled while the command reports success. Without a check, every
    later test fails against an empty database and is blamed on the model
    while the results still stamp reset_per_test."""
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))

    environment.reset()

    script = _script(calls[0])
    assert "wp core is-installed" in script
    assert script.index("wp db import") < script.index("wp core is-installed")


def test_reset_error_renders_a_safe_command_string() -> None:
    """Naive joining of an `sh -c` invocation produces a string that resets
    the operator's own WordPress if pasted into a host shell."""
    environment, _ = _env(GraderConfig(kind="docker"), ("", "boom", 1, False))

    with pytest.raises(RuntimeError) as excinfo:
        environment.reset()

    assert "sh -c 'wp db reset" in str(excinfo.value)


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
    assert "wp db import -" in _script(calls[0])


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
    assert "wp db export -" in script


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

    assert "wp db export -" in script


def test_capture_feeds_exactly_what_restore_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bytes captured at setup are the bytes every reset imports.

    They travel over stdin rather than a file in the runtime, so nothing
    between capture and restore can alter them.
    """
    dump = "-- MariaDB dump\nINSERT INTO wp_options VALUES (1);\n"
    environment, calls = _env(GraderConfig(kind="docker"), (dump, "", 0, False), baseline=None)
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    environment.setup()
    environment.reset()

    assert environment._baseline == dump
    assert calls[1][1] == dump


def test_baseline_is_never_written_into_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate code is eval'd in that container as root, so a dump on disk
    there is a file the graded code could truncate (silently emptying every
    later reset) or append rows to (silently pre-seeding every later test)."""
    environment, calls = _env(GraderConfig(kind="docker"), ("-- dump\n", "", 0, False), baseline=None)
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    environment.setup()
    environment.reset()

    for command, _ in calls:
        script = command[2]
        assert ".sql" not in script, f"baseline written into the runtime: {script!r}"
        assert ">" not in script.replace(">/dev/null", ""), f"redirect to a file: {script!r}"


def test_reset_refuses_without_a_captured_baseline() -> None:
    """Restoring nothing would drop every table and leave WordPress
    uninstalled while the results still stamp reset_per_test."""
    environment, _ = _env(GraderConfig(kind="docker"), ("ok", "", 0, False), baseline=None)

    with pytest.raises(RuntimeError, match="No clean baseline"):
        environment.reset()


def test_empty_capture_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty dump imports cleanly and exits 0, so it must be caught at
    capture or every test in the run grades against an empty database."""
    environment, _ = _env(GraderConfig(kind="docker"), ("   \n", "", 0, False), baseline=None)
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    with pytest.raises(RuntimeError, match="empty WordPress baseline"):
        environment.setup()


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


# Capture only when the run will actually restore ------------------------


def test_setup_skips_capture_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Capturing runs `wp db reset`, which destroys whatever is in the
    database. A run that never calls reset() (execution_isolation 'none')
    must not pay that cost, and must not wipe state the caller kept."""
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    environment.setup(capture_baseline=False)

    assert calls == []


def test_setup_captures_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaulting to True keeps any caller that resets safe by omission."""
    environment, calls = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)

    environment.setup()

    assert len(calls) == 1


def test_setup_still_starts_the_runtime_without_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the capture must not skip bringing the runtime up."""
    environment, _ = _env(GraderConfig(kind="docker"), ("ok", "", 0, False))
    started: list[bool] = []
    monkeypatch.setattr(environment, "_container_exists", lambda: False)
    monkeypatch.setattr(environment, "_start_container", lambda: started.append(True))

    environment.setup(capture_baseline=False)

    assert started == [True]
