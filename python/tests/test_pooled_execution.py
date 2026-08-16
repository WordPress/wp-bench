"""Tests for the pooled execution loop.

test_database_pooling.py proves the shell strings name the right database.
These prove the loop hands them out correctly: that a test's reset and its
grading always name the same slot, and that no slot is ever held by two live
tests at once. Both are properties of concurrent behavior, so they are
exercised against real threads rather than asserted by inspection.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from wp_bench.config import (
    DatasetConfig,
    GraderConfig,
    HarnessConfig,
    ModelConfig,
    OutputConfig,
    RunConfig,
)
from wp_bench.core import BenchmarkRunner
from wp_bench.datasets import ExecutionTest
from wp_bench.environment import ExecutionResult

#: How long a barrier waits before declaring the pool never got that many
#: tests in flight. Generous enough for a loaded CI box, short enough that a
#: broken pool fails instead of hanging the suite.
BARRIER_TIMEOUT_SECONDS = 30


def _execution_test(test_id: str) -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category="general",
        difficulty="basic",
        requirements=["Requirement"],
        test_function=None,
        static_checks={},
        runtime_checks={
            "assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]
        },
        reference_solution="function ref() { return true; }",
        metadata={},
    )


def _passing_result() -> ExecutionResult:
    raw = {
        "success": True,
        "static": {"score": 1.0, "details": {"total_weight": 1}},
        "runtime": {"score": 1.0, "details": {"total_weight": 1}},
    }
    return ExecutionResult(success=True, raw=raw, stdout="", stderr="")


def _config(tmp_path: Path, **run_overrides: Any) -> HarnessConfig:
    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(check_reference_solution=True, **run_overrides),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )


class PoolSpy:
    """A stand-in runtime that records which slot every call used.

    One instance stands in for the single shared WordPressEnvironment, so a
    worker index that leaked onto instance state instead of travelling with
    the task would show up here as mismatched reset/execute pairs.

    A slot counts as *live* from its reset until its grading returns — the
    exact window in which two tests sharing it would corrupt each other. The
    barrier holds every grading open until ``parties`` of them are in flight,
    so the overlap is real rather than hoped for.
    """

    def __init__(self, parties: int = 1) -> None:
        self._lock = threading.Lock()
        self._live: set[int] = set()
        self._barrier = threading.Barrier(parties)
        #: Slots that were live twice over: the failure this pool exists to
        #: make impossible.
        self.shared_slots: list[int] = []
        self.events: list[tuple[str, int]] = []
        self.peak_live = 0
        self.worker_count: int | None = None
        self.capture_baseline: bool | None = None

    def setup(self, *, capture_baseline: bool = True, worker_count: int = 1) -> None:
        self.worker_count = worker_count
        self.capture_baseline = capture_baseline

    def reset(self, worker: int = 0) -> None:
        with self._lock:
            if worker in self._live:
                self.shared_slots.append(worker)
            self._live.add(worker)
            self.peak_live = max(self.peak_live, len(self._live))
            self.events.append(("reset", worker))

    def execute_artifact(
        self,
        artifact: object,
        verification_spec: dict,
        worker: int = 0,
    ) -> ExecutionResult:
        # Block before recording, so a run that handed one slot to several
        # tests shows up as a run of resets with no grading between them
        # rather than as a tidy alternation.
        self._barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        with self._lock:
            self.events.append(("execute", worker))
            self._live.discard(worker)
        return _passing_result()

    def slots_used(self) -> set[int]:
        return {worker for _, worker in self.events}

    def timeline(self, worker: int) -> list[str]:
        return [call for call, slot in self.events if slot == worker]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tests: int,
    concurrency: int,
    parties: int | None = None,
) -> tuple[dict[str, Any], PoolSpy]:
    """Run reference-solution mode (no model calls) against a PoolSpy."""
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [_execution_test(f"e-{index}") for index in range(tests)],
    )
    runner = BenchmarkRunner(_config(tmp_path, execution_concurrency=concurrency))
    spy = PoolSpy(parties=concurrency if parties is None else parties)
    runner.environment = spy  # type: ignore[assignment]
    return runner.run(), spy


# Exclusivity -----------------------------------------------------------


def test_concurrent_tests_never_share_a_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The whole point of the pool. Two live tests on one slot would mean
    each grading against state the other is mutating, while the results file
    still stamped reset_per_test."""
    _, spy = _run(monkeypatch, tmp_path, tests=12, concurrency=4)

    assert spy.shared_slots == []


def test_the_pool_actually_runs_tests_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Guards the test above from passing vacuously: with no real overlap,
    no exclusivity violation could ever be observed."""
    _, spy = _run(monkeypatch, tmp_path, tests=12, concurrency=4)

    assert spy.peak_live == 4


def test_every_worker_slot_gets_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pool that provisioned four databases and then graded everything on
    one would be paying for isolation it is not using."""
    _, spy = _run(monkeypatch, tmp_path, tests=12, concurrency=4)

    assert spy.slots_used() == {0, 1, 2, 3}


def test_a_slot_is_reset_immediately_before_each_of_its_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Per slot, the timeline must strictly alternate. A second reset before
    a grading would mean another test claimed the slot mid-flight; a grading
    with no reset before it would mean a test ran on the previous test's
    leftovers."""
    _, spy = _run(monkeypatch, tmp_path, tests=12, concurrency=4)

    for worker in sorted(spy.slots_used()):
        timeline = spy.timeline(worker)
        assert timeline == ["reset", "execute"] * (len(timeline) // 2), (
            f"worker {worker} timeline was {timeline}"
        )


def test_every_test_gets_its_own_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, spy = _run(monkeypatch, tmp_path, tests=12, concurrency=4)

    assert [call for call, _ in spy.events].count("reset") == 12


def test_slots_are_returned_so_more_tests_than_workers_still_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A slot not returned to the queue deadlocks the run once the pool has
    handed out its last database."""
    payload, spy = _run(monkeypatch, tmp_path, tests=12, concurrency=4)

    assert len(payload["results"]) == 12
    assert [call for call, _ in spy.events].count("execute") == 12


# Provisioning and metadata ---------------------------------------------


def test_setup_provisions_one_database_per_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, spy = _run(monkeypatch, tmp_path, tests=4, concurrency=4)

    assert spy.worker_count == 4


def test_metadata_records_the_pooling_strategy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pooled run is still reset_per_test and must say so — but a reader
    comparing two result files is entitled to know which kind they hold."""
    payload, _ = _run(monkeypatch, tmp_path, tests=4, concurrency=4)

    assert payload["metadata"]["runtime_isolation"] == "reset_per_test"
    assert payload["metadata"]["execution_concurrency"] == 4
    assert payload["metadata"]["isolation_pooling"] == "database_per_worker"


def test_serial_metadata_does_not_claim_pooling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload, _ = _run(monkeypatch, tmp_path, tests=2, concurrency=1)

    assert payload["metadata"]["runtime_isolation"] == "reset_per_test"
    assert payload["metadata"]["execution_concurrency"] == 1
    assert payload["metadata"]["isolation_pooling"] == "single_database"


# The serial path is unchanged ------------------------------------------


def test_serial_runs_stay_on_the_runtimes_own_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """W=1 must be the pre-pooling run: worker 0 everywhere, so no worker
    database is created and every command is the one it always was."""
    _, spy = _run(monkeypatch, tmp_path, tests=3, concurrency=1)

    assert spy.slots_used() == {0}
    assert spy.worker_count == 1


def test_serial_runs_stay_serial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, spy = _run(monkeypatch, tmp_path, tests=3, concurrency=1)

    assert spy.peak_live == 1
    assert spy.events == [("reset", 0), ("execute", 0)] * 3


# Failure handling ------------------------------------------------------


class ExplodingResetSpy(PoolSpy):
    """Fails one worker's reset, the way a dropped MySQL connection would."""

    def __init__(self, failing_worker: int) -> None:
        super().__init__(parties=1)
        self.failing_worker = failing_worker

    def reset(self, worker: int = 0) -> None:
        if worker == self.failing_worker:
            raise RuntimeError("Reset command failed with exit code 1")
        super().reset(worker)


def test_a_failed_reset_on_any_worker_aborts_the_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reset that failed leaves that slot dirty. continue_on_error covers
    per-test failures, not a runtime that is no longer known-clean, so the
    run must stop rather than keep grading against it."""
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [_execution_test(f"e-{index}") for index in range(12)],
    )
    runner = BenchmarkRunner(
        _config(tmp_path, execution_concurrency=4, continue_on_error=True)
    )
    runner.environment = ExplodingResetSpy(failing_worker=2)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Reset command failed"):
        runner.run()


def test_a_failed_reset_stops_the_pool_from_grading_everything(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Not just 'it raised' — the queue must actually drain rather than run
    to completion and raise at the end."""
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [_execution_test(f"e-{index}") for index in range(40)],
    )
    runner = BenchmarkRunner(_config(tmp_path, execution_concurrency=4))
    spy = ExplodingResetSpy(failing_worker=2)
    runner.environment = spy  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="Reset command failed"):
        runner.run()

    graded = [call for call, _ in spy.events].count("execute")
    assert graded < 40, f"the run graded all {graded} tests after a reset failed"
