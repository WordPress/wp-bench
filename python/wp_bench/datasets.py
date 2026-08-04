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
    test_type: str
    category: str
    difficulty: str
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


@dataclass
class KnowledgeTest:
    id: str
    suite: str
    prompt: str
    test_type: str
    category: str
    difficulty: str
    choices: list[dict[str, Any]] | None = None
    correct_answer: str | None = None
    answer_type: str | None = None
    answer: str | None = None
    metadata: dict[str, Any] | None = None


def load_tests(config: DatasetConfig) -> dict[str, list[Any]]:
    """Load both execution and knowledge tests for a suite."""
    if config.source == "huggingface":
        return _load_from_huggingface(config)
    return _load_from_local_files(config)


def filter_tests_by_ids(
    tests: dict[str, list[Any]],
    test_ids: list[str],
) -> dict[str, list[Any]]:
    """Filter loaded tests to the requested dataset test IDs."""
    if not test_ids:
        return tests

    available_ids = {test.id for group in tests.values() for test in group}
    missing_ids = [test_id for test_id in test_ids if test_id not in available_ids]
    if missing_ids:
        raise ValueError(f"Unknown test id(s): {', '.join(missing_ids)}")

    wanted = set(test_ids)
    return {
        kind: [test for test in group if test.id in wanted]
        for kind, group in tests.items()
    }


def ensure_test_ids_match_type(
    tests: dict[str, list[Any]],
    test_type: str | None,
    test_ids: list[str],
) -> None:
    """Fail clearly when requested IDs do not match an explicit test type."""
    if not test_ids or test_type is None:
        return
    if not tests[test_type]:
        raise ValueError(
            f"No {test_type} tests matched requested test id(s): {', '.join(test_ids)}"
        )


def _load_from_huggingface(config: DatasetConfig) -> dict[str, list[Any]]:
    """Load dataset from Hugging Face Hub (Parquet format)."""
    dataset = hf_load_dataset(
        config.name,
        revision=config.revision,
        split=config.split,
        cache_dir=str(config.cache_dir) if config.cache_dir else None,
    )
    execution: list[ExecutionTest] = []
    knowledge: list[KnowledgeTest] = []

    for row in dataset:
        # Parse JSON-encoded fields from Parquet format
        requirements = _parse_json_field(row.get("requirements", "[]"))
        static_checks = _parse_json_field(row.get("static_checks", "{}"))
        runtime_checks = _parse_json_field(row.get("runtime_checks", "{}"))
        choices = _parse_json_field(row.get("choices", "[]"))

        if row.get("test_kind") == "execution":
            execution.append(
                ExecutionTest(
                    id=row["id"],
                    suite=row.get("suite", config.name),
                    prompt=row["prompt"],
                    expected_behavior=row.get("expected_behavior", ""),
                    test_type="execution",
                    category=row.get("category", "general"),
                    difficulty=row.get("difficulty", "unknown"),
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
        else:
            choice_list = choices if isinstance(choices, list) and choices else None
            knowledge.append(
                KnowledgeTest(
                    id=row["id"],
                    suite=row.get("suite", config.name),
                    prompt=row["prompt"],
                    test_type=row.get("type", "knowledge"),
                    category=row.get("category", "general"),
                    difficulty=row.get("difficulty", "unknown"),
                    choices=choice_list,
                    correct_answer=row.get("correct_answer"),
                    answer_type=row.get("answer_type"),
                    metadata=_row_metadata(row),
                )
            )
    return {"execution": execution, "knowledge": knowledge}


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


def _load_from_local_files(config: DatasetConfig) -> dict[str, list[Any]]:
    suite = config.name.split("/")[-1]
    suite_dir = DATASET_SUITES_DIR / suite

    execution: list[ExecutionTest] = []
    knowledge: list[KnowledgeTest] = []

    # Load all execution test files from execution/ directory
    execution_dir = suite_dir / "execution"
    if execution_dir.is_dir():
        for path in sorted(execution_dir.glob("*.json")):
            execution.extend(_parse_execution_suite(path))

    # Load all knowledge test files from knowledge/ directory
    knowledge_dir = suite_dir / "knowledge"
    if knowledge_dir.is_dir():
        for path in sorted(knowledge_dir.glob("*.json")):
            knowledge.extend(_parse_knowledge_suite(path))

    if config.split != "test":
        raise ValueError("Local dataset loader only supports the 'test' split")
    return {"execution": execution, "knowledge": knowledge}


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
                test_type="execution",
                category=test.get("category", "general"),
                difficulty=test.get("difficulty", "unknown"),
                requirements=test.get("requirements", []),
                test_function=test.get("test_function"),
                static_checks=test.get("static_checks", {}),
                runtime_checks=test.get("runtime_checks", {}),
                reference_solution=test.get("reference_solution"),
                metadata=_merge_metadata(test.get("metadata", {}), metadata),
                artifact_kind=test.get("artifact_kind", "php_snippet"),
                reference_files=test.get("reference_files"),
            )
        )
    return tests


def _parse_knowledge_suite(path: Path) -> list[KnowledgeTest]:
    data = _read_json(path)
    suite_id = data.get("id", path.stem)
    metadata = data.get("metadata", {})
    tests = []
    for test in data.get("tests", []):
        tests.append(
            KnowledgeTest(
                id=test["id"],
                suite=suite_id,
                prompt=test["prompt"],
                test_type=test.get("type", "knowledge"),
                category=test.get("category", "general"),
                difficulty=test.get("difficulty", "unknown"),
                choices=test.get("choices"),
                correct_answer=test.get("correct_answer"),
                answer_type=test.get("answer_type"),
                metadata=_merge_metadata(test.get("metadata", {}), metadata),
            )
        )
    return tests


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return orjson.loads(handle.read())
