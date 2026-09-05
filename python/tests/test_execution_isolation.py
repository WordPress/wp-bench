"""Tests for execution-test state isolation (reset_per_test strategy)."""
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
from wp_bench.core import BenchmarkRunner, MultiModelRunner
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


class SpyEnvironment:
    """Records the interleaving of reset and execute calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def setup(self, *, capture_baseline: bool = True) -> None:
        self.calls.append("setup")
        self.capture_baseline = capture_baseline

    def reset(self) -> None:
        self.calls.append("reset")

    def execute_artifact(self, artifact: object, verification_spec: dict) -> ExecutionResult:
        self.calls.append("execute")
        return _passing_result()


def test_execution_runner_resets_between_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every execution test is preceded by an environment reset."""
    tests = [_execution_test("e-one"), _execution_test("e-two"), _execution_test("e-three")]
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: tests,
    )
    config = _config(tmp_path)
    runner = BenchmarkRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )

    runner.run()

    assert spy.calls == [
        "setup",
        "reset", "execute",
        "reset", "execute",
        "reset", "execute",
    ]


def test_reference_solution_mode_resets_between_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reference-solution mode uses the same isolation path."""
    tests = [_execution_test("e-one"), _execution_test("e-two")]
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: tests,
    )
    config = _config(tmp_path, check_reference_solution=True)
    runner = BenchmarkRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]

    runner.run()

    assert spy.calls == ["setup", "reset", "execute", "reset", "execute"]


def test_multi_model_runner_resets_between_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """State from one model run cannot leak into the next model's tests."""
    tests = [_execution_test("e-one")]
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: tests,
    )
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        models=[ModelConfig(name="model-a"), ModelConfig(name="model-b")],
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )
    runner = MultiModelRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]
    monkeypatch.setattr(
        "wp_bench.core.ModelInterface",
        lambda model_config, system_prompt=None: type(
            "FakeModel",
            (),
            {"generate_with_metadata": staticmethod(lambda prompt: fake_generation("```php\ncode\n```"))},
        )(),
    )

    runner.run()

    # Each model's only test is preceded by its own reset: no state carries over.
    assert spy.calls == ["setup", "reset", "execute", "reset", "execute"]


def test_concurrency_above_one_rejected_for_reset_per_test() -> None:
    """reset_per_test isolation cannot support concurrent execution tests."""
    with pytest.raises(ValueError, match="execution_concurrency must be 1"):
        RunConfig(execution_isolation="reset_per_test", execution_concurrency=4)


def test_isolation_none_allows_concurrency() -> None:
    """Legacy concurrent mode is available only by explicit opt-out."""
    config = RunConfig(execution_isolation="none", execution_concurrency=4)
    assert config.execution_concurrency == 4


def test_execution_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        RunConfig(execution_isolation="none", execution_concurrency=0)


def test_result_metadata_records_isolation_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Isolation strategy is auditable from result metadata."""
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [_execution_test("e-one")],
    )
    config = _config(tmp_path, categories=["general"])
    runner = BenchmarkRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )

    payload = runner.run()

    assert payload["metadata"]["runtime_isolation"] == "reset_per_test"
    assert payload["metadata"]["categories"] == ["general"]


def test_result_metadata_test_ids_take_precedence_over_categories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [_execution_test("e-one")],
    )
    config = _config(tmp_path, categories=["general"], test_ids=["e-one"])
    runner = BenchmarkRunner(config)
    runner.environment = SpyEnvironment()  # type: ignore[assignment]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )

    payload = runner.run()

    assert payload["metadata"]["categories"] == []


def test_isolation_none_still_runs_all_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Opting out of isolation preserves the legacy concurrent path."""
    tests = [_execution_test("e-one"), _execution_test("e-two")]
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: tests,
    )
    config = _config(tmp_path, execution_isolation="none", execution_concurrency=2)
    runner = BenchmarkRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )

    payload = runner.run()

    assert spy.calls.count("execute") == 2
    assert spy.calls.count("reset") == 0
    assert payload["metadata"]["runtime_isolation"] == "none"


def test_isolation_none_does_not_capture_a_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Capturing a baseline runs `wp db reset`. A run that never restores one
    must not destroy database state the caller deliberately kept."""
    tests = [_execution_test("e-one")]
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: tests)
    config = _config(tmp_path, execution_isolation="none", execution_concurrency=2)
    runner = BenchmarkRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )

    runner.run()

    assert spy.capture_baseline is False


def test_reset_per_test_captures_a_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tests = [_execution_test("e-one")]
    monkeypatch.setattr("wp_bench.core.load_tests", lambda dataset: tests)
    config = _config(tmp_path)
    runner = BenchmarkRunner(config)
    spy = SpyEnvironment()
    runner.environment = spy  # type: ignore[assignment]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )

    runner.run()

    assert spy.capture_baseline is True
