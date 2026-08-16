from __future__ import annotations

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
from wp_bench.records import execution_record_passed


def _config(tmp_path: Path, test_ids: list[str] | None = None) -> HarnessConfig:
    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(
            check_reference_solution=True,
            test_ids=test_ids or [],
            # A cli grader cannot reset WordPress, so per-test isolation is
            # not available here; the runner path under test does not need it.
            execution_isolation="none",
        ),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )


def _execution_test(test_id: str = "e-one") -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="Reviewer contract: expected",
        category="general",
        difficulty="basic",
        requirements=["Requirement"],
        test_function=None,
        static_checks={"required_patterns": [{"pattern": "ref", "weight": 1}]},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
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


def _failing_result() -> ExecutionResult:
    raw = {
        "success": False,
        "static": {"score": 1.0, "details": {"total_weight": 1}},
        "runtime": {"score": 0.0, "details": {"total_weight": 1}},
    }
    return ExecutionResult(success=False, raw=raw, stdout="", stderr="")


def test_reference_solution_mode_executes_reference_solution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test = _execution_test()
    config = _config(tmp_path)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: [test])
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda **kwargs: None  # type: ignore[method-assign]

    def fake_execute_artifact(artifact: Any, verification_spec: dict, worker: int = 0) -> ExecutionResult:
        calls.append((artifact.code, verification_spec))
        return _passing_result()

    runner.environment.execute_artifact = fake_execute_artifact  # type: ignore[method-assign]

    result = runner.run()

    assert calls == [
        (
            test.reference_solution,
            {
                "static_checks": test.static_checks,
                "runtime_checks": test.runtime_checks,
            },
        )
    ]
    assert result["metadata"]["mode"] == "reference_solution"
    assert result["metadata"]["scores"]["correctness"] == 1.0
    record = result["results"][0]
    assert record["mode"] == "reference_solution"
    assert record["model"] is None
    assert execution_record_passed(record) is True


def test_reference_solution_mode_exits_nonzero_on_failed_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test = _execution_test()
    config = _config(tmp_path)
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: [test])
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda **kwargs: None  # type: ignore[method-assign]

    def fake_execute_artifact(artifact: Any, verification_spec: dict[str, Any], worker: int = 0) -> ExecutionResult:
        return _failing_result()

    runner.environment.execute_artifact = fake_execute_artifact  # type: ignore[method-assign]

    with pytest.raises(SystemExit) as exc:
        runner.run()

    assert exc.value.code == 1
    assert execution_record_passed(runner.records[0]) is False
    # v2 scoring: runtime is the behavioral signal; a full static match no
    # longer contributes correctness credit when runtime fails.
    assert runner.records[0]["scores"]["correctness"] == 0.0
    assert runner.records[0]["scores"]["static"] == 1.0


def test_reference_solution_mode_rejects_unknown_test_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [_execution_test()],
    )
    runner = BenchmarkRunner(_config(tmp_path, test_ids=["e-missing"]))

    with pytest.raises(ValueError, match="Unknown test id"):
        runner.run()
