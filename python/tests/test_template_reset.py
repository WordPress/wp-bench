"""A host-held SQLite baseline must restore a clean site before every test."""
from __future__ import annotations

import base64
from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.environment import (
    EnvironmentSetupTimeout,
    ProcessResult,
    WordPressEnvironment,
)

FAKE_BASELINE = base64.b64encode(b"SQLite format 3\0" + bytes(512)).decode()


def _env(
    config: GraderConfig,
    result: tuple[str, str, int, bool],
    *,
    baseline: str | None = FAKE_BASELINE,
):
    environment = WordPressEnvironment(config)
    calls: list[tuple[list[str], str | None]] = []

    def fake_exec(command: list[str], *, stdin: str | None = None, **kwargs: Any):
        calls.append((command, stdin))
        return result

    environment._exec = fake_exec  # type: ignore[method-assign]
    environment._baseline = baseline
    environment._run_process = lambda *args, **kwargs: ProcessResult("", "", 0, False)  # type: ignore[method-assign]
    environment._wait_for_container = lambda: None  # type: ignore[method-assign]
    environment._require_sqlite = lambda: None  # type: ignore[method-assign]
    return environment, calls


def _script(call: tuple[list[str], str | None]) -> str:
    command = call[0]
    assert command[:2] == ["sh", "-c"]
    return command[2]


def test_reset_restores_without_reinstalling_in_one_round_trip() -> None:
    environment, calls = _env(GraderConfig(), ("ok", "", 0, False))
    environment.reset()
    assert len(calls) == 1
    script = _script(calls[0])
    assert "sqlite-snapshot.php import" in script
    assert "wp core install" not in script
    assert "wp db" not in script
    assert script.endswith("import && wp core is-installed")
    assert calls[0][1] == FAKE_BASELINE


def test_reset_error_renders_a_safe_command_string() -> None:
    environment, _ = _env(GraderConfig(), ("", "boom", 1, False))
    with pytest.raises(RuntimeError) as excinfo:
        environment.reset()
    assert "sh -c 'php " in str(excinfo.value)


def _captured_script(monkeypatch: pytest.MonkeyPatch, config: GraderConfig) -> str:
    environment, calls = _env(config, (FAKE_BASELINE, "", 0, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    environment.setup()
    assert len(calls) == 1
    return _script(calls[0])


def test_setup_captures_a_clean_install(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _captured_script(monkeypatch, GraderConfig())
    assert script.index("clear") < script.index("wp core install") < script.index("export")
    assert "wp db" not in script


def test_capture_uses_configured_site_url(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _captured_script(monkeypatch, GraderConfig(base_url="http://wp-bench.example:9999"))
    assert "--url=http://wp-bench.example:9999" in script


def test_capture_feeds_exactly_what_restore_replays(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, calls = _env(GraderConfig(), (FAKE_BASELINE, "", 0, False), baseline=None)
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    environment.setup()
    environment.reset()
    assert environment._baseline == FAKE_BASELINE
    assert calls[1][1] == FAKE_BASELINE


def test_baseline_is_not_left_in_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, calls = _env(GraderConfig(), (FAKE_BASELINE, "", 0, False), baseline=None)
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    environment.setup()
    environment.reset()
    for command, _ in calls:
        script = command[2]
        assert ">" not in script.replace(">/dev/null", "")


def test_reset_refuses_without_baseline() -> None:
    environment, _ = _env(GraderConfig(), ("ok", "", 0, False), baseline=None)
    with pytest.raises(RuntimeError, match="No clean baseline"):
        environment.reset()


def test_failed_capture_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _ = _env(GraderConfig(), ("", "no space left on device", 1, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    with pytest.raises(RuntimeError, match="baseline.*no space left on device"):
        environment.setup()


def test_capture_timeout_aborts_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _ = _env(GraderConfig(), ("", "", 0, True))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    with pytest.raises(EnvironmentSetupTimeout):
        environment.setup()


def test_failed_restore_raises() -> None:
    environment, _ = _env(GraderConfig(), ("", "database is locked", 1, False))
    with pytest.raises(RuntimeError, match="database is locked"):
        environment.reset()


def test_restore_timeout_raises() -> None:
    environment, _ = _env(GraderConfig(), ("", "", 0, True))
    with pytest.raises(EnvironmentSetupTimeout, match="Timed out resetting"):
        environment.reset()


def test_cli_grader_refuses_reset_and_captures_no_baseline() -> None:
    environment, calls = _env(GraderConfig(kind="cli"), ("ok", "", 0, False), baseline=None)
    environment.setup()
    assert calls == []
    assert environment._baseline is None
    with pytest.raises(RuntimeError, match="no reset implementation"):
        environment.reset()


def test_setup_skips_capture_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, calls = _env(GraderConfig(), (FAKE_BASELINE, "", 0, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    environment.setup(capture_baseline=False)
    assert calls == []


def test_setup_captures_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, calls = _env(GraderConfig(), (FAKE_BASELINE, "", 0, False))
    monkeypatch.setattr(environment, "_container_exists", lambda: True)
    environment.setup()
    assert len(calls) == 1


def test_setup_starts_runtime_without_capturing(monkeypatch: pytest.MonkeyPatch) -> None:
    environment, _ = _env(GraderConfig(), ("ok", "", 0, False))
    started: list[bool] = []
    monkeypatch.setattr(environment, "_container_exists", lambda: False)
    monkeypatch.setattr(environment, "_start_container", lambda: started.append(True))
    environment.setup(capture_baseline=False)
    assert started == [True]
