"""Canonical per-test result records shared by every run mode.

Single-model, multi-model, and reference-solution runs must produce
structurally identical per-test records so any WP-Bench run can serve as a
leaderboard artifact. Build records exclusively through the helpers here;
do not hand-roll record dicts in runner code.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .config import ModelConfig

#: Bump when the per-test record shape changes. Recorded in payload metadata.
RESULT_SCHEMA_VERSION = "1.0"


def _model_info(model_config: Optional[ModelConfig]) -> Optional[Dict[str, Any]]:
    """Serialize the model configuration relevant to reproducibility."""
    if model_config is None:
        return None
    return {
        "name": model_config.name,
        "kind": model_config.kind,
        "temperature": model_config.temperature,
        "top_p": model_config.top_p,
        "max_tokens": model_config.max_tokens,
    }


def _empty_usage() -> Dict[str, Any]:
    """Usage placeholders; populated when usage capture is implemented."""
    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "latency_ms": None,
    }


def _dimension_score(raw: Optional[Dict[str, Any]], dimension: str) -> Optional[float]:
    """Extract a dimension score (runtime/static) from the raw grader result."""
    if not isinstance(raw, dict):
        return None
    result = raw.get(dimension)
    if not isinstance(result, dict):
        return None
    score = result.get("score")
    return float(score) if isinstance(score, (int, float)) else None


def build_knowledge_record(
    *,
    test: Any,
    mode: str,
    model_config: Optional[ModelConfig],
    prompt_hash: str,
    raw_completion: str,
    answer: str,
    knowledge_score: float,
) -> Dict[str, Any]:
    """Build the canonical record for a knowledge test result."""
    return {
        "test_id": test.id,
        "suite": test.suite,
        "type": "knowledge",
        "category": test.category,
        "difficulty": test.difficulty,
        "mode": mode,
        "prompt_hash": prompt_hash,
        "model": _model_info(model_config),
        "output": {
            "raw_completion": raw_completion,
            "code": None,
            "answer": answer,
        },
        "scores": {
            "knowledge": knowledge_score,
            "correctness": None,
            "runtime": None,
            "static": None,
        },
        "grader": None,
        "usage": _empty_usage(),
        "error": None,
    }


def build_execution_record(
    *,
    test: Any,
    mode: str,
    model_config: Optional[ModelConfig],
    prompt_hash: Optional[str],
    raw_completion: Optional[str],
    code: str,
    env_result: Any,
    correctness: float,
) -> Dict[str, Any]:
    """Build the canonical record for an execution test result."""
    raw = env_result.raw or {}
    return {
        "test_id": test.id,
        "suite": test.suite,
        "type": "execution",
        "category": test.category,
        "difficulty": test.difficulty,
        "mode": mode,
        "prompt_hash": prompt_hash,
        "model": _model_info(model_config),
        "output": {
            "raw_completion": raw_completion,
            "code": code,
            "answer": None,
        },
        "scores": {
            "knowledge": None,
            "correctness": correctness,
            "runtime": _dimension_score(raw, "runtime"),
            "static": _dimension_score(raw, "static"),
        },
        "grader": {
            "success": env_result.success,
            "raw": raw,
            "stdout": env_result.stdout,
            "stderr": env_result.stderr,
            "timeout": bool(raw.get("timeout", False)) or getattr(env_result, "timed_out", False),
        },
        "usage": _empty_usage(),
        "error": None,
    }


def execution_record_passed(record: Dict[str, Any]) -> bool:
    """Whether an execution record represents a strict pass.

    Used by reference-solution mode to decide failures: the grader must
    report success and correctness must be effectively 1.0.
    """
    grader = record.get("grader") or {}
    correctness = (record.get("scores") or {}).get("correctness")
    return bool(grader.get("success")) and isinstance(correctness, (int, float)) and correctness >= 0.999


def sort_records(records: list) -> list:
    """Order records deterministically for stable output diffs."""
    return sorted(records, key=lambda record: (record.get("type", ""), record.get("test_id", "")))
