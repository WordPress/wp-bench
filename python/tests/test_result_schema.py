"""Tests asserting the canonical per-test record schema across run modes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from wp_bench.config import (
    DatasetConfig,
    GraderConfig,
    HarnessConfig,
    ModelConfig,
    OutputConfig,
    RunConfig,
)
from wp_bench.core import BenchmarkRunner, SingleModelRunner
from wp_bench.datasets import ExecutionTest, KnowledgeTest
from wp_bench.environment import ExecutionResult
from wp_bench.records import RESULT_SCHEMA_VERSION


def _execution_test(test_id: str = "e-one") -> ExecutionTest:
    return ExecutionTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        test_type="execution",
        category="hooks",
        difficulty="basic",
        requirements=["Requirement"],
        test_function=None,
        static_checks={"required_patterns": [{"pattern": "ref", "weight": 1}]},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
        reference_solution="function ref() { return true; }",
        metadata={},
    )


def _knowledge_test(test_id: str = "k-one") -> KnowledgeTest:
    return KnowledgeTest(
        id=test_id,
        suite="wp-core-v1",
        prompt="What hook runs on init?",
        test_type="knowledge",
        category="hooks",
        difficulty="basic",
        correct_answer="init",
        answer_type="short_answer",
    )


def _passing_result() -> ExecutionResult:
    raw = {
        "success": True,
        "static": {"score": 1.0, "details": {"total_weight": 1}},
        "runtime": {"score": 1.0, "details": {"total_weight": 1}},
    }
    return ExecutionResult(success=True, raw=raw, stdout="out", stderr="")


def _key_paths(record: Dict[str, Any], prefix: str = "") -> Set[str]:
    """Flatten a record into dotted key paths for structural comparison."""
    paths: Set[str] = set()
    for key, value in record.items():
        path = f"{prefix}{key}"
        paths.add(path)
        if isinstance(value, dict) and key != "raw":
            paths.update(_key_paths(value, prefix=f"{path}."))
    return paths


def _single_model_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list:
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "single.json", jsonl_path=None),
    )
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: {"execution": [_execution_test()], "knowledge": [_knowledge_test()]},
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    runner.environment.execute_code = lambda code, verification_spec: _passing_result()  # type: ignore[method-assign]
    monkeypatch.setattr(runner.model, "generate", lambda prompt: "init")
    runner.run()
    return runner.records


def _multi_model_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list:
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        models=[ModelConfig(name="model-a")],
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "multi.json", jsonl_path=None),
    )

    class FakeEnvironment:
        def setup(self) -> None: ...
        def reset(self) -> None: ...

        def execute_code(self, code: str, spec: dict) -> ExecutionResult:
            return _passing_result()

    runner = SingleModelRunner(
        config=config,
        model_config=config.get_models()[0],
        environment=FakeEnvironment(),  # type: ignore[arg-type]
        tests={"execution": [_execution_test()], "knowledge": [_knowledge_test()]},
    )
    monkeypatch.setattr(runner.model, "generate", lambda prompt: "init")
    runner.run()
    return runner.records


def test_single_and_multi_model_records_have_same_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    single = _single_model_records(monkeypatch, tmp_path)
    multi = _multi_model_records(monkeypatch, tmp_path)

    single_by_type = {record["type"]: record for record in single}
    multi_by_type = {record["type"]: record for record in multi}

    for test_type in ("knowledge", "execution"):
        assert _key_paths(single_by_type[test_type]) == _key_paths(multi_by_type[test_type]), (
            f"{test_type} record shape differs between single- and multi-model modes"
        )


def test_multi_model_execution_records_include_audit_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = _multi_model_records(monkeypatch, tmp_path)
    execution = next(record for record in records if record["type"] == "execution")

    assert execution["prompt_hash"]
    assert execution["category"] == "hooks"
    assert execution["difficulty"] == "basic"
    assert execution["mode"] == "model"
    assert execution["grader"]["raw"]["runtime"]["score"] == 1.0
    assert execution["grader"]["stdout"] == "out"
    assert execution["output"]["raw_completion"] == "init"


def test_reference_solution_record_uses_canonical_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(check_reference_solution=True),
        output=OutputConfig(path=tmp_path / "ref.json", jsonl_path=None),
    )
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: {"execution": [_execution_test()], "knowledge": []},
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    runner.environment.execute_code = lambda code, verification_spec: _passing_result()  # type: ignore[method-assign]

    runner.run()
    record = runner.records[0]
    single = _single_model_records(monkeypatch, tmp_path)
    model_execution = next(r for r in single if r["type"] == "execution")

    assert record["mode"] == "reference_solution"
    assert record["model"] is None
    # model is intentionally null in reference mode, so nested model.* paths
    # exist only in model mode; the record shape must match otherwise.
    reference_paths = {p for p in _key_paths(record) if not p.startswith("model.")}
    model_paths = {p for p in _key_paths(model_execution) if not p.startswith("model.")}
    assert reference_paths == model_paths


def test_jsonl_records_match_json_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=tmp_path / "results.jsonl"),
    )
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: {"execution": [_execution_test()], "knowledge": [_knowledge_test()]},
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    runner.environment.execute_code = lambda code, verification_spec: _passing_result()  # type: ignore[method-assign]
    monkeypatch.setattr(runner.model, "generate", lambda prompt: "init")

    runner.run()

    json_files = sorted(tmp_path.glob("results_*.json"))
    jsonl_files = sorted(tmp_path.glob("results_*.jsonl"))
    assert json_files and jsonl_files
    payload = json.loads(json_files[-1].read_text())
    jsonl_records = [
        json.loads(line) for line in jsonl_files[-1].read_text().splitlines() if line
    ]

    assert payload["results"] == jsonl_records
    assert payload["metadata"]["result_schema_version"] == RESULT_SCHEMA_VERSION


def test_records_are_sorted_for_stable_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(test_type="execution"),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )
    # Deliberately out-of-order test IDs.
    tests = [_execution_test("e-zebra"), _execution_test("e-alpha"), _execution_test("e-mid")]
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: {"execution": tests, "knowledge": []},
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]
    runner.environment.execute_code = lambda code, verification_spec: _passing_result()  # type: ignore[method-assign]
    monkeypatch.setattr(runner.model, "generate", lambda prompt: "```php\ncode\n```")

    payload = runner.run()

    ids = [record["test_id"] for record in payload["results"]]
    assert ids == ["e-alpha", "e-mid", "e-zebra"]
