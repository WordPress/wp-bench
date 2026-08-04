"""Tests for per-test metadata preservation across loaders, export, and records."""
from __future__ import annotations

import sys
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
from wp_bench.datasets import (
    ExecutionTest,
    _merge_metadata,
    _row_metadata,
    load_tests,
)
from wp_bench.environment import ExecutionResult

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_local_parser_preserves_execution_task_metadata() -> None:
    """Per-task provenance fields survive local loading at the top level."""
    tests = load_tests(DatasetConfig(source="local", name="wp-core-v1"))

    with_refs = [t for t in tests if t.metadata.get("source_refs")]
    assert with_refs, "expected execution tasks with source_refs metadata"
    sample = with_refs[0]
    assert isinstance(sample.metadata["source_refs"], list)
    assert "release_focus" in sample.metadata
    # Suite metadata is preserved alongside, not instead of, task metadata.
    assert isinstance(sample.metadata.get("suite_metadata"), dict)


def test_merge_metadata_prefers_task_fields_and_nests_suite() -> None:
    merged = _merge_metadata(
        {"source_refs": ["a.php"], "release_focus": "6.9"},
        {"wp_version": "7.0"},
    )
    assert merged["source_refs"] == ["a.php"]
    assert merged["release_focus"] == "6.9"
    assert merged["suite_metadata"] == {"wp_version": "7.0"}


def test_merge_metadata_tolerates_non_dict_input() -> None:
    assert _merge_metadata(None, None) == {"suite_metadata": {}}
    assert _merge_metadata("bad", ["bad"]) == {"suite_metadata": {}}


def test_row_metadata_parses_json_string() -> None:
    row = {"metadata": '{"source_refs": ["x.php"], "release_focus": "classic"}'}
    parsed = _row_metadata(row)
    assert parsed["source_refs"] == ["x.php"]
    assert parsed["release_focus"] == "classic"


def test_row_metadata_handles_missing_or_invalid() -> None:
    assert _row_metadata({}) == {}
    assert _row_metadata({"metadata": "not json"}) == {}


def test_huggingface_loader_preserves_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mocked HF rows round-trip the metadata column into dataclasses."""
    rows = [
        {
            "id": "e-one",
            "suite": "wp-core-v1",
            "test_kind": "execution",
            "prompt": "Prompt",
            "requirements": "[]",
            "static_checks": "{}",
            "runtime_checks": "{}",
            "choices": "[]",
            "metadata": '{"source_refs": ["plugin.php"], "release_focus": "6.9"}',
        },
        {
            "id": "k-one",
            "suite": "wp-core-v1",
            "test_kind": "knowledge",
            "prompt": "Prompt",
            "requirements": "[]",
            "static_checks": "{}",
            "runtime_checks": "{}",
            "choices": "[]",
            "correct_answer": "init",
            "answer_type": "short_answer",
            "metadata": '{"review_status": "reviewed"}',
        },
    ]
    monkeypatch.setattr("wp_bench.datasets.hf_load_dataset", lambda *a, **k: rows)

    tests = load_tests(DatasetConfig(source="huggingface", name="WordPress/wp-bench-v1"))

    # Legacy knowledge rows in older parquet exports are skipped.
    assert [test.id for test in tests] == ["e-one"]
    assert tests[0].metadata["source_refs"] == ["plugin.php"]
    assert tests[0].metadata["release_focus"] == "6.9"


def test_export_dataset_writes_metadata_column() -> None:
    """Exported rows carry the per-task metadata as a JSON column."""
    sys.path.insert(0, str(PROJECT_ROOT / "datasets"))
    try:
        from export_dataset import load_suite  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    rows = load_suite("wp-core-v1")

    assert rows, "expected exported rows for wp-core-v1"
    assert all("metadata" in row for row in rows)
    execution_rows = [r for r in rows if r["test_kind"] == "execution"]
    assert any('"source_refs"' in r["metadata"] for r in execution_rows)


def test_result_record_includes_task_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Task provenance is auditable from every result record."""
    test = ExecutionTest(
        id="e-one",
        suite="wp-core-v1",
        prompt="Prompt",
        expected_behavior="expected",
        test_type="execution",
        category="general",
        difficulty="basic",
        requirements=[],
        test_function=None,
        static_checks={},
        runtime_checks={"assertions": [{"type": "custom_assertion", "code": "return true;", "weight": 1}]},
        reference_solution=None,
        metadata={"source_refs": ["plugin.php"], "release_focus": "6.9", "suite_metadata": {}},
    )
    config = HarnessConfig(
        dataset=DatasetConfig(source="local", name="wp-core-v1"),
        model=ModelConfig(name="test-model"),
        grader=GraderConfig(kind="cli"),
        run=RunConfig(),
        output=OutputConfig(path=tmp_path / "results.json", jsonl_path=None),
    )
    monkeypatch.setattr(
        "wp_bench.core.load_tests",
        lambda dataset: [test],
    )
    runner = BenchmarkRunner(config)
    runner.environment.setup = lambda: None  # type: ignore[method-assign]
    runner.environment.reset = lambda: None  # type: ignore[method-assign]

    def fake_execute(artifact: object, verification_spec: dict) -> ExecutionResult:
        raw: dict[str, Any] = {
            "success": True,
            "runtime": {"score": 1.0, "details": {"total_weight": 1}},
        }
        return ExecutionResult(success=True, raw=raw, stdout="", stderr="")

    runner.environment.execute_artifact = fake_execute  # type: ignore[method-assign]
    monkeypatch.setattr(
        runner.model, "generate_with_metadata", lambda prompt: fake_generation("```php\ncode\n```")
    )

    payload = runner.run()

    record = payload["results"][0]
    assert record["metadata"]["source_refs"] == ["plugin.php"]
    assert record["metadata"]["release_focus"] == "6.9"
