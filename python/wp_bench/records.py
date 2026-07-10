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


def build_knowledge_record(
    *,
    test: Any,
    mode: str,
    model_config: Optional[ModelConfig],
    prompt_hash: str,
    raw_completion: str,
    answer: str,
    knowledge_score: float,
    usage: Optional[Dict[str, Any]] = None,
    model_call: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the canonical record for a knowledge test result."""
    return {
        "test_id": test.id,
        "suite": test.suite,
        "type": "knowledge",
        "category": test.category,
        "difficulty": test.difficulty,
        "metadata": getattr(test, "metadata", None) or {},
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
            "execution_pass": None,
            "runtime": None,
            "static": None,
            "static_policy_pass": None,
        },
        "grader": None,
        "usage": usage if usage is not None else _empty_usage(),
        "model_call": model_call,
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
    scores: Dict[str, Any],
    usage: Optional[Dict[str, Any]] = None,
    model_call: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the canonical record for an execution test result.

    Args:
        scores: Scores object from BenchmarkRunner._score_execution, carrying
            correctness (legacy), execution_pass (primary), runtime, static,
            and static_policy_pass.
    """
    raw = env_result.raw or {}
    return {
        "test_id": test.id,
        "suite": test.suite,
        "type": "execution",
        "category": test.category,
        "difficulty": test.difficulty,
        "metadata": getattr(test, "metadata", None) or {},
        "mode": mode,
        "prompt_hash": prompt_hash,
        "model": _model_info(model_config),
        "output": {
            "raw_completion": raw_completion,
            "code": code,
            "answer": None,
        },
        "scores": scores,
        "grader": {
            "success": env_result.success,
            "raw": raw,
            "stdout": env_result.stdout,
            "stderr": env_result.stderr,
            "timeout": bool(raw.get("timeout", False)) or getattr(env_result, "timed_out", False),
        },
        "usage": usage if usage is not None else _empty_usage(),
        "model_call": model_call,
        "error": None,
    }


def execution_record_passed(record: Dict[str, Any]) -> bool:
    """Whether an execution record represents a strict pass.

    Reads the primary execution_pass metric (SCORING_VERSION 2.0). Used by
    reference-solution mode to decide failures.
    """
    return bool((record.get("scores") or {}).get("execution_pass"))


def sort_records(records: list) -> list:
    """Order records deterministically for stable output diffs."""
    return sorted(records, key=lambda record: (record.get("type", ""), record.get("test_id", "")))
