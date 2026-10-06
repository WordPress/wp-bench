"""Lifecycle regressions for private candidate runtimes and concurrent aborts."""
from __future__ import annotations

import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from wp_bench.config import GraderConfig
from wp_bench.core import _ContinueOnErrorPolicy, _run_concurrent_loop
from wp_bench.environment import ProcessResult, WordPressEnvironment


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch):
    env = WordPressEnvironment(GraderConfig(container_name="operator-runtime"))
    commands: list[list[str]] = []

    def process(command: list[str], **kwargs: Any) -> ProcessResult:
        commands.append(command)
        stdout = "sha256:pristine" if command[:3] == ["docker", "image", "inspect"] else ""
        return ProcessResult(stdout, "", 0, False)

    def execute(command: list[str], **kwargs: Any):
        if command == ["cat", "wp-config.php"]:
            return "<?php // pristine config", "", 0, False
        if command[:2] == ["sh", "-c"] and " export" in command[2]:
            return base64.b64encode(b"SQLite format 3\0" + bytes(512)).decode(), "", 0, False
        return '{"success":true}', "", 0, False

    monkeypatch.setattr(env, "_run_process", process)
    monkeypatch.setattr(env, "_exec", execute)
    monkeypatch.setattr(env, "_wait_for_container", lambda: None)
    env.setup()
    yield env, commands
    env.close()


def test_setup_and_candidates_do_not_touch_existing_runtime(environment) -> None:
    env, commands = environment
    for _ in range(2):
        env.reset()
        assert env.execute_code("code", {}).success
    starts = [command for command in commands if command[:3] == ["docker", "run", "-d"]]
    names = [command[command.index("--name") + 1] for command in starts]
    assert len(names) == len(set(names)) == 3  # pristine install + two candidates
    assert all("operator-runtime" not in command for command in commands)
    assert all("sha256:pristine" in command for command in starts)
    assert env.image_id == "sha256:pristine"
    assert env._owned == {}


def test_overlapping_candidates_use_distinct_runtime_names(environment, monkeypatch) -> None:
    env, _ = environment
    barrier = threading.Barrier(4)
    seen: list[str] = []

    def execute(command: list[str], **kwargs: Any):
        if command[:2] == ["wp", "eval-file"]:
            seen.append(env._runtime_container())
            barrier.wait(timeout=5)
        return '{"success":true}', "", 0, False

    monkeypatch.setattr(env, "_exec", execute)

    def candidate(_: int) -> None:
        env.reset()
        assert env.execute_code("code", {}).success

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(candidate, range(8)))
    assert len(set(seen)) == 8
    assert env._owned == {}


@pytest.mark.parametrize("failure", ["restore", "timeout", "crash"])
def test_failed_candidates_are_removed_before_more_work(environment, monkeypatch, failure) -> None:
    env, _ = environment
    if failure == "restore":
        monkeypatch.setattr(env, "_exec", lambda *args, **kwargs: ("", "corrupt baseline", 1, False))
        with pytest.raises(RuntimeError, match="corrupt baseline"):
            env.reset()
    else:
        env.reset()
        monkeypatch.setattr(env, "_exec", lambda *args, **kwargs: ("partial", "", 1, failure == "timeout"))
        result = env.execute_code("code", {})
        assert not result.success
        assert result.timed_out == (failure == "timeout")
    assert env._owned == {}


def test_cleanup_failure_is_reported_and_can_be_retried(environment, monkeypatch) -> None:
    env, _ = environment
    env.reset()
    original = env._run_process
    monkeypatch.setattr(env, "_run_process", lambda *args, **kwargs: ProcessResult("", "daemon unavailable", 1, False))
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        env.close()
    assert len(env._owned) == 1
    monkeypatch.setattr(env, "_run_process", original)
    env.close()
    assert env._owned == {}
    with pytest.raises(RuntimeError, match="stopped"):
        env.reset()


def test_abort_waits_for_in_flight_container_creation(environment, monkeypatch) -> None:
    env, _ = environment
    starting = threading.Event()
    proceed = threading.Event()
    created = threading.Event()
    closing = threading.Event()
    original = env._run_process

    def process(command: list[str], **kwargs: Any) -> ProcessResult:
        if command[:3] == ["docker", "run", "-d"]:
            starting.set()
            assert proceed.wait(timeout=5)
            created.set()
        if command[:3] == ["docker", "rm", "-f"]:
            assert created.is_set()
        return original(command, **kwargs)

    monkeypatch.setattr(env, "_run_process", process)

    def close() -> None:
        closing.set()
        env.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        start = executor.submit(env._start_isolated_container, install=False)
        assert starting.wait(timeout=5)
        stop = executor.submit(close)
        assert closing.wait(timeout=5)
        proceed.set()
        start.result(timeout=5)
        stop.result(timeout=5)
    assert env._owned == {}


def test_interrupt_while_waiting_cancels_queued_tests(monkeypatch) -> None:
    """Catch Ctrl-C in as_completed, before executor shutdown can drain work."""
    barrier = threading.Barrier(3)
    release = threading.Event()
    started: list[int] = []

    def process(test: int) -> dict:
        started.append(test)
        barrier.wait(timeout=5)
        assert release.wait(timeout=5)
        return {"test": test}

    def interrupted(futures):
        barrier.wait(timeout=5)
        raise KeyboardInterrupt

    monkeypatch.setattr("wp_bench.core.as_completed", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _run_concurrent_loop(
            tests_to_run=list(range(8)), max_workers=2, progress_label="Tests",
            process_test=process, on_result=lambda result: None,
            on_error=lambda test, error: None, policy=_ContinueOnErrorPolicy(False),
            on_abort=release.set,
        )
    assert sorted(started) == [0, 1]
