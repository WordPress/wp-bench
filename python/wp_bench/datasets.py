"""Dataset loading utilities for WP-Bench."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
from datasets import load_dataset as hf_load_dataset  # type: ignore[attr-defined]

from .config import DatasetConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_SUITES_DIR = PROJECT_ROOT / "datasets" / "suites"


@dataclass
class ExecutionTest:
    id: str
    suite: str
    prompt: str
    expected_behavior: str
    category: str
    requirements: list[str]
    test_function: str | None
    static_checks: dict[str, Any]
    runtime_checks: dict[str, Any]
    reference_solution: str | None
    metadata: dict[str, Any]
    #: What the model must produce: 'php_snippet' (default) or
    #: 'wp_plugin_files' (JSON files map installed as a plugin).
    artifact_kind: str = "php_snippet"
    #: Reference files for wp_plugin_files reference-solution runs.
    reference_files: dict[str, str] | None = None
    #: Authored cheat snippets that must FAIL the assertions; the inverse of
    #: reference_solution, run by --check-exploits alongside the generic
    #: battery. Maintainer-side QA data — excluded from the parquet export.
    exploit_solutions: list[str] | None = None


def load_tests(config: DatasetConfig) -> list[ExecutionTest]:
    """Load the execution tests for a suite."""
    if config.source == "huggingface":
        return _load_from_huggingface(config)
    return _load_from_local_files(config)


def filter_tests_by_ids(
    tests: list[ExecutionTest],
    test_ids: list[str],
) -> list[ExecutionTest]:
    """Filter loaded tests to the requested dataset test IDs."""
    if not test_ids:
        return tests

    available_ids = {test.id for test in tests}
    missing_ids = [test_id for test_id in test_ids if test_id not in available_ids]
    if missing_ids:
        raise ValueError(f"Unknown test id(s): {', '.join(missing_ids)}")

    wanted = set(test_ids)
    return [test for test in tests if test.id in wanted]


def _load_from_huggingface(config: DatasetConfig) -> list[ExecutionTest]:
    """Load dataset from Hugging Face Hub (Parquet format).

    Rows without ``test_kind == "execution"`` (legacy knowledge rows in
    older parquet exports) are skipped.
    """
    dataset = hf_load_dataset(
        config.name,
        revision=config.revision,
        split=config.split,
        cache_dir=str(config.cache_dir) if config.cache_dir else None,
    )
    execution: list[ExecutionTest] = []

    for row in dataset:
        if row.get("test_kind") != "execution":
            continue
        # Parse JSON-encoded fields from Parquet format
        requirements = _parse_json_field(row.get("requirements", "[]"))
        static_checks = _parse_json_field(row.get("static_checks", "{}"))
        runtime_checks = _parse_json_field(row.get("runtime_checks", "{}"))
        execution.append(
            ExecutionTest(
                id=row["id"],
                suite=row.get("suite", config.name),
                prompt=row["prompt"],
                expected_behavior=row.get("expected_behavior", ""),
                category=row.get("category", "general"),
                requirements=requirements if isinstance(requirements, list) else [],
                test_function=row.get("test_function") or None,
                static_checks=static_checks if isinstance(static_checks, dict) else {},
                runtime_checks=runtime_checks if isinstance(runtime_checks, dict) else {},
                reference_solution=row.get("reference_solution"),
                metadata=_row_metadata(row),
                artifact_kind=row.get("artifact_kind") or "php_snippet",
                reference_files=_parse_optional_dict(row.get("reference_files")),
            )
        )
    return execution


def _parse_json_field(value: Any) -> Any:
    """Parse a JSON-encoded string field, or return as-is if already parsed."""
    if isinstance(value, str):
        try:
            return orjson.loads(value)
        except (orjson.JSONDecodeError, TypeError):
            return value
    return value


def _merge_metadata(task_metadata: Any, suite_metadata: Any) -> dict[str, Any]:
    """Combine per-task and suite-level metadata without losing either.

    Task fields (source_refs, release_focus, version targets, ...) sit at
    the top level for easy querying; suite metadata is nested under
    ``suite_metadata``. A task field named ``suite_metadata`` would be
    shadowed, which is acceptable and documented in the dataset README.
    """
    merged: dict[str, Any] = dict(task_metadata) if isinstance(task_metadata, dict) else {}
    merged["suite_metadata"] = suite_metadata if isinstance(suite_metadata, dict) else {}
    return merged


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Parse the metadata column from a Hugging Face row."""
    parsed = _parse_json_field(row.get("metadata", "{}"))
    return parsed if isinstance(parsed, dict) else {}


def _parse_optional_dict(value: Any) -> dict[str, Any] | None:
    """Parse an optional JSON-encoded dict column; empty means None."""
    parsed = _parse_json_field(value) if value else None
    return parsed if isinstance(parsed, dict) and parsed else None


def _load_from_local_files(config: DatasetConfig) -> list[ExecutionTest]:
    suite = config.name.split("/")[-1]
    suite_dir = DATASET_SUITES_DIR / suite

    execution: list[ExecutionTest] = []

    execution_dir = suite_dir / "execution"
    if execution_dir.is_dir():
        for path in sorted(execution_dir.glob("*.json")):
            execution.extend(_parse_execution_suite(path))

    if config.split != "test":
        raise ValueError("Local dataset loader only supports the 'test' split")
    return execution


def _parse_execution_suite(path: Path) -> list[ExecutionTest]:
    data = _read_json(path)
    suite_id = data.get("id", path.stem)
    metadata = data.get("metadata", {})
    tests = []
    for test in data.get("tests", []):
        tests.append(
            ExecutionTest(
                id=test["id"],
                suite=suite_id,
                prompt=test["prompt"],
                expected_behavior=test.get("expected_behavior", ""),
                category=test.get("category", "general"),
                requirements=test.get("requirements", []),
                test_function=test.get("test_function"),
                static_checks=test.get("static_checks", {}),
                runtime_checks=test.get("runtime_checks", {}),
                reference_solution=test.get("reference_solution"),
                metadata=_merge_metadata(test.get("metadata", {}), metadata),
                artifact_kind=test.get("artifact_kind", "php_snippet"),
                reference_files=test.get("reference_files"),
                exploit_solutions=test.get("exploit_solutions"),
            )
        )
    return tests


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return orjson.loads(handle.read())
