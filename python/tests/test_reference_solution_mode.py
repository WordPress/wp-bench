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
from wp_bench.datasets import ExecutionTest, KnowledgeTest
from wp_bench.environment import ExecutionResult


def _config(tmp_path: Path, test_ids: list[str] | None = None) -> HarnessConfig:
    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(
            check_reference_solution=True,
            concurrency=1,
            test_ids=test_ids or [],
        ),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )


def _execution_test(test_id: str = "e-one") -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="Reviewer contract: expected",
        test_type="execution",
        category="general",
        difficulty="basic",
        requirements=["Requirement"],
        static_checks={"required_patterns": []},
        runtime_checks={"assertions": []},
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
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: {"execution": [test], "knowledge": []},
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]

    def fake_execute_code(code: str, verification_spec: dict) -> ExecutionResult:
        calls.append((code, verification_spec))
        return _passing_result()

    runner.environment.execute_code = fake_execute_code  # type: ignore[method-assign]

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
    assert result["results"][0]["passed"] is True


def test_reference_solution_mode_exits_nonzero_on_failed_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test = _execution_test()
    config = _config(tmp_path)
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: {"execution": [test], "knowledge": []},
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]

    def fake_execute_code(code: str, verification_spec: dict[str, Any]) -> ExecutionResult:
        return _failing_result()

    runner.environment.execute_code = fake_execute_code  # type: ignore[method-assign]

    with pytest.raises(SystemExit) as exc:
        runner.run()

    assert exc.value.code == 1
    assert runner.records[0]["passed"] is False
    assert runner.records[0]["correctness"] == 0.5


def test_reference_solution_mode_rejects_non_execution_test_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    knowledge_test = KnowledgeTest(
        id="k-one",
        suite="wp-core-v1",
        prompt="Prompt",
        test_type="knowledge",
        category="general",
        difficulty="basic",
    )
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: {"execution": [], "knowledge": [knowledge_test]},
    )
    runner = BenchmarkRunner(_config(tmp_path, test_ids=["k-one"]))

    with pytest.raises(ValueError, match="only supports execution"):
        runner.run()
