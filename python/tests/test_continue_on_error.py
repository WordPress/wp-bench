"""Tests for run.continue_on_error: record per-test errors, keep going."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import fake_generation

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


def _execution_test(test_id: str) -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        category="general",
        requirements=["Requirement"],
        test_function=None,
        static_checks={},
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


def _config(tmp_path: Path, **run_overrides: Any) -> HarnessConfig:
    return HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(**run_overrides),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )


class QuietEnvironment:
    """Environment stub that always passes."""

    def setup(self, *, capture_baseline: bool = True) -> None:
        pass

    def reset(self) -> None:
        pass

    def execute_artifact(self, artifact: object, verification_spec: dict) -> ExecutionResult:
        return _passing_result()


def _model_failing_on(bad_prompts_substring: str, good_text: str = "```php\ncode\n```"):
    """generate_with_metadata stub erroring when the prompt names a test."""

    def generate(prompt: str):
        if bad_prompts_substring in prompt:
            raise RuntimeError("provider exploded")
        return fake_generation(good_text)

    return generate


def _runner_with_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    execution: list[ExecutionTest] | None = None,
    **run_overrides: Any,
) -> BenchmarkRunner:
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: execution or [],
    )
    runner = BenchmarkRunner(_config(tmp_path, **run_overrides))
    runner.environment = QuietEnvironment()  # type: ignore[assignment]
    return runner


def test_default_off_error_still_aborts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without the flag, the first per-test error aborts the run (legacy)."""
    tests = [_execution_test("e-one"), _execution_test("e-two")]
    # Distinct prompts so the stub can target one test.
    tests[1].prompt = "BAD Prompt"
    runner = _runner_with_tests(monkeypatch, tmp_path, execution=tests)
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", _model_failing_on("BAD")
    )

    with pytest.raises(SystemExit):
        runner.run()


def test_execution_error_is_recorded_and_run_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An errored execution test becomes a canonical record with error set."""
    tests = [_execution_test("e-one"), _execution_test("e-two"), _execution_test("e-three")]
    tests[1].prompt = "BAD Prompt"
    runner = _runner_with_tests(
        monkeypatch, tmp_path, execution=tests, continue_on_error=True,
    )
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", _model_failing_on("BAD")
    )

    payload = runner.run()

    records = {record["test_id"]: record for record in payload["results"]}
    assert len(records) == 3
    errored = records["e-two"]
    assert errored["error"] == {"type": "RuntimeError", "message": "provider exploded"}
    assert errored["scores"]["execution_pass"] is None
    assert records["e-one"]["error"] is None
    # Errored record keeps the exact canonical key structure.
    assert set(errored.keys()) == set(records["e-one"].keys())


def test_errored_tests_excluded_from_aggregates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ungraded test has no score: pass rate covers graded tests only."""
    tests = [_execution_test("e-one"), _execution_test("e-two"), _execution_test("e-three")]
    tests[1].prompt = "BAD Prompt"
    runner = _runner_with_tests(
        monkeypatch, tmp_path, execution=tests, continue_on_error=True,
    )
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", _model_failing_on("BAD")
    )

    payload = runner.run()

    scores = payload["metadata"]["scores"]
    # Two graded tests passed; the errored one neither passes nor fails.
    assert scores["execution_pass_rate"] == 1.0


def test_metadata_records_errored_test_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Result metadata makes skipped-by-error tests auditable."""
    tests = [_execution_test("e-one"), _execution_test("e-two")]
    tests[0].prompt = "BAD Prompt"
    runner = _runner_with_tests(
        monkeypatch, tmp_path, execution=tests, continue_on_error=True,
    )
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", _model_failing_on("BAD")
    )

    payload = runner.run()

    assert payload["metadata"]["continue_on_error"] is True
    assert payload["metadata"]["errored_test_ids"] == ["e-one"]


def test_concurrent_execution_path_continues_on_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The legacy concurrent path (isolation none) honors the flag too."""
    tests = [_execution_test("e-one"), _execution_test("e-two"), _execution_test("e-three")]
    tests[2].prompt = "BAD Prompt"
    runner = _runner_with_tests(
        monkeypatch, tmp_path, execution=tests,
        continue_on_error=True,
        execution_isolation="none", execution_concurrency=2,
    )
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", _model_failing_on("BAD")
    )

    payload = runner.run()

    records = {record["test_id"]: record for record in payload["results"]}
    assert len(records) == 3
    assert records["e-three"]["error"] is not None


def test_systemic_failure_still_aborts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """continue_on_error must not burn the suite when every test errors."""
    tests = [_execution_test(f"e-{index}") for index in range(8)]
    runner = _runner_with_tests(
        monkeypatch, tmp_path, execution=tests, continue_on_error=True,
    )

    def always_fail(prompt: str):
        raise RuntimeError("bad credentials")

    monkeypatch.setattr(runner.model, "generate_with_metadata", always_fail)

    with pytest.raises(SystemExit):
        runner.run()

    # Aborted at the systemic threshold, not after burning all 8 tests.
    assert len(runner.records) < len(tests)


def test_all_error_run_aborts_even_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A small run whose every test errors must not write a null-score file."""
    tests = [_execution_test("e-one"), _execution_test("e-two")]
    runner = _runner_with_tests(
        monkeypatch, tmp_path, execution=tests, continue_on_error=True,
    )

    def always_fail(prompt: str):
        raise RuntimeError("bad credentials")

    monkeypatch.setattr(runner.model, "generate_with_metadata", always_fail)

    with pytest.raises(SystemExit):
        runner.run()


def test_success_after_errors_disarms_systemic_abort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One graded test proves the setup works; later errors are per-test."""
    tests = [_execution_test(f"e-{index}") for index in range(8)]
    tests[0].prompt = "GOOD Prompt"
    for test in tests[1:]:
        test.prompt = "BAD Prompt"
    runner = _runner_with_tests(
        monkeypatch, tmp_path, execution=tests, continue_on_error=True,
    )
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", _model_failing_on("BAD")
    )

    payload = runner.run()

    assert len(payload["results"]) == 8
    assert len(payload["metadata"]["errored_test_ids"]) == 7
