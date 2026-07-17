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


def _null_scores() -> Dict[str, Any]:
    """The full score key set, all null — a record that carries no score."""
    return {
        "knowledge": None,
        "correctness": None,
        "execution_pass": None,
        "runtime": None,
        "static": None,
        "static_policy_pass": None,
    }


def _base_record(
    *,
    test: Any,
    test_type: str,
    mode: str,
    model_config: Optional[ModelConfig],
) -> Dict[str, Any]:
    """The canonical per-test record skeleton shared by every builder.

    Every field defaults to its null/empty form; each builder overrides only
    the ones its record populates. Centralizing the key structure here keeps
    the builders from drifting (the key set is asserted identical in tests).
    """
    return {
        "test_id": test.id,
        "suite": test.suite,
        "type": test_type,
        "category": test.category,
        "difficulty": test.difficulty,
        "metadata": getattr(test, "metadata", None) or {},
        "mode": mode,
        "prompt_hash": None,
        "model": _model_info(model_config),
        "output": {"raw_completion": None, "code": None, "answer": None},
        "scores": _null_scores(),
        "grader": None,
        "usage": _empty_usage(),
        "model_call": None,
        "error": None,
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
    record = _base_record(test=test, test_type="knowledge", mode=mode, model_config=model_config)
    record["prompt_hash"] = prompt_hash
    record["output"] = {"raw_completion": raw_completion, "code": None, "answer": answer}
    record["scores"]["knowledge"] = knowledge_score
    record["usage"] = usage if usage is not None else _empty_usage()
    record["model_call"] = model_call
    return record


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
    record = _base_record(test=test, test_type="execution", mode=mode, model_config=model_config)
    record["prompt_hash"] = prompt_hash
    record["output"] = {"raw_completion": raw_completion, "code": code, "answer": None}
    record["scores"] = scores
    record["grader"] = {
        "success": env_result.success,
        "raw": raw,
        "stdout": env_result.stdout,
        "stderr": env_result.stderr,
        "timeout": bool(raw.get("timeout", False)) or getattr(env_result, "timed_out", False),
    }
    record["usage"] = usage if usage is not None else _empty_usage()
    record["model_call"] = model_call
    return record


def build_error_record(
    *,
    test: Any,
    test_type: str,
    mode: str,
    model_config: Optional[ModelConfig],
    error_type: str,
    error_message: str,
) -> Dict[str, Any]:
    """Build the canonical record for a test that errored before grading.

    Used by ``run.continue_on_error``: the record fills the reserved
    ``error`` field, carries null scores (an ungraded test has no score,
    pass or fail), and keeps the exact canonical key structure so consumers
    never need a separate parser for errored tests.
    """
    record = _base_record(test=test, test_type=test_type, mode=mode, model_config=model_config)
    record["error"] = {"type": error_type, "message": error_message}
    return record


def build_exploit_audit_record(
    *,
    test: Any,
    candidates_tried: int,
    passing_exploit: Optional[str],
    exploit_code: Optional[str],
) -> Dict[str, Any]:
    """Build the record for one test in the adversarial assertion audit.

    Shares the canonical header (test_id/suite/type/category/difficulty)
    with the other builders; the tail is audit-specific — an exploit
    outcome rather than scores. ``candidates_tried == 0`` means the generic
    battery did not cover the test (no gateway function, or a plugin
    artifact), not that it is safe. A non-null ``passing_exploit`` means a
    zero-effort cheat satisfied the assertions.
    """
    return {
        "test_id": test.id,
        "suite": test.suite,
        "type": "execution",
        "category": test.category,
        "difficulty": test.difficulty,
        "candidates_tried": candidates_tried,
        "exploitable": passing_exploit is not None,
        "passing_exploit": passing_exploit,
        "exploit_code": exploit_code,
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


def errored_test_ids(records: list) -> list:
    """Sorted ids of records that errored (run.continue_on_error)."""
    return sorted(record["test_id"] for record in records if record.get("error") is not None)
