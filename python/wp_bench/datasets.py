"""Dataset loading utilities for WP-Bench."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import orjson
from datasets import load_dataset as hf_load_dataset

from .config import DatasetConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_SUITES_DIR = PROJECT_ROOT / "datasets" / "suites"


@dataclass
class ExecutionTest:
    id: str
    suite: str
    prompt: str
    test_type: str
    category: str
    difficulty: str
    requirements: List[str]
    static_checks: Dict[str, Any]
    runtime_checks: Dict[str, Any]
    judge_config: Optional[Dict[str, Any]]
    reference_solution: Optional[str]
    metadata: Dict[str, Any]


@dataclass
class KnowledgeTest:
    id: str
    suite: str
    prompt: str
    test_type: str
    category: str
    difficulty: str
    choices: Optional[List[Dict[str, Any]]] = None
    correct_answer: Optional[str] = None
    answer: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


def load_tests(config: DatasetConfig) -> Dict[str, List[Any]]:
    """Load both execution and knowledge tests for a suite."""
    if config.source == "huggingface":
        return _load_from_huggingface(config)
    return _load_from_local_files(config)


def _load_from_huggingface(config: DatasetConfig) -> Dict[str, List[Any]]:
    """Load dataset from Hugging Face Hub (Parquet format)."""
    dataset = hf_load_dataset(
        config.name,
        revision=config.revision,
        split=config.split,
        cache_dir=str(config.cache_dir) if config.cache_dir else None,
    )
    execution: List[ExecutionTest] = []
    knowledge: List[KnowledgeTest] = []

    for row in dataset:
        # Parse JSON-encoded fields from Parquet format
        requirements = _parse_json_field(row.get("requirements", "[]"))
        static_checks = _parse_json_field(row.get("static_checks", "{}"))
        runtime_checks = _parse_json_field(row.get("runtime_checks", "{}"))
        judge_config = _parse_json_field(row.get("judge_config", "{}"))
        choices = _parse_json_field(row.get("choices", "[]"))

        if row.get("test_kind") == "execution":
            execution.append(
                ExecutionTest(
                    id=row["id"],
                    suite=row.get("suite", config.name),
                    prompt=row["prompt"],
                    test_type="execution",
                    category=row.get("category", "general"),
                    difficulty=row.get("difficulty", "unknown"),
                    requirements=requirements if isinstance(requirements, list) else [],
                    static_checks=static_checks if isinstance(static_checks, dict) else {},
                    runtime_checks=runtime_checks if isinstance(runtime_checks, dict) else {},
                    judge_config=judge_config if isinstance(judge_config, dict) else None,
                    reference_solution=row.get("reference_solution"),
                    metadata={},
                )
            )
        else:
            knowledge.append(
                KnowledgeTest(
                    id=row["id"],
                    suite=row.get("suite", config.name),
                    prompt=row["prompt"],
                    test_type="knowledge",
                    category=row.get("category", "general"),
                    difficulty=row.get("difficulty", "unknown"),
                    choices=choices if isinstance(choices, list) else None,
                    correct_answer=row.get("correct_answer"),
                    metadata={},
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


def _load_from_local_files(config: DatasetConfig) -> Dict[str, List[Any]]:
    suite = config.name.split("/")[-1]
    execution_file = DATASET_SUITES_DIR / suite / "execution.json"
    knowledge_file = DATASET_SUITES_DIR / suite / "knowledge.json"

    execution = _parse_execution_suite(execution_file)
    knowledge = _parse_knowledge_suite(knowledge_file)
    if config.split != "test":
        raise ValueError("Local dataset loader only supports the 'test' split")
    return {"execution": execution, "knowledge": knowledge}


def _parse_execution_suite(path: Path) -> List[ExecutionTest]:
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
                test_type="execution",
                category=test.get("category", "general"),
                difficulty=test.get("difficulty", "unknown"),
                requirements=test.get("requirements", []),
                static_checks=test.get("static_checks", {}),
                runtime_checks=test.get("runtime_checks", {}),
                judge_config=test.get("judge_config"),
                reference_solution=test.get("reference_solution"),
                metadata={"suite_metadata": metadata},
            )
        )
    return tests


def _parse_knowledge_suite(path: Path) -> List[KnowledgeTest]:
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
                metadata={"suite_metadata": metadata},
            )
        )
    return tests


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("rb") as handle:
        return orjson.loads(handle.read())
