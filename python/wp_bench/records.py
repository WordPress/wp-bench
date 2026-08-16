"""Canonical per-test result records shared by every run mode.

Single-model, multi-model, and reference-solution runs must produce
structurally identical per-test records so any WP-Bench run can serve as a
leaderboard artifact. Build records exclusively through the helpers here;
do not hand-roll record dicts in runner code.
"""
from __future__ import annotations

from typing import Any

from .config import ModelConfig

#: Bump when the per-test record shape changes. Recorded in payload metadata.
#: 2.0: the "knowledge" score key and the knowledge-only "output.answer"
#: field were removed (knowledge track removed).
#: 2.1: added the "variant" block for skill-injection A/B runs.
RESULT_SCHEMA_VERSION = "2.1"


def isolation_metadata(isolation: str, concurrency: int) -> dict[str, Any]:
    """How a run actually isolated its execution tests.

    ``runtime_isolation`` alone cannot tell a serial ``reset_per_test`` run
    from a pooled one, and the two reach isolation by different means: a
    serial run time-slices one database, a pooled run hands every concurrent
    test a database no other test touches. Both are honestly
    ``reset_per_test`` and both must say so, but a reader comparing two
    result files is entitled to know which one they are holding.

    Args:
        isolation: The configured ``run.execution_isolation``.
        concurrency: The most tests this run could have had in flight at
            once -- the configured ceiling, lowered to the number of tests
            actually selected. A bound, not an observation: a run whose
            tests fail in milliseconds may never reach it. Run modes with
            their own serial loop pass 1 regardless of what the config asked
            for, because serial is what they are.
    """
    pooled = isolation == "reset_per_test" and concurrency > 1
    return {
        "runtime_isolation": isolation,
        "execution_concurrency": concurrency,
        "isolation_pooling": "database_per_worker" if pooled else "single_database",
    }


def _baseline_variant_info() -> dict[str, Any]:
    """The no-skills variant identity every record carries by default.

    Kept structurally identical to skills.Variant.record_info() so the
    variant key paths never diverge between baseline and skills records.
    """
    return {"key": "baseline", "kind": "none", "system_prompt_hash": None}


def _model_info(model_config: ModelConfig | None) -> dict[str, Any] | None:
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


def _empty_usage() -> dict[str, Any]:
    """Usage placeholders; populated when usage capture is implemented."""
    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "latency_ms": None,
    }


def _null_scores() -> dict[str, Any]:
    """The full score key set, all null — a record that carries no score."""
    return {
        "correctness": None,
        "execution_pass": None,
        "runtime": None,
        "static": None,
        "static_policy_pass": None,
    }


def _base_record(
    *,
    test: Any,
    mode: str,
    model_config: ModelConfig | None,
    variant: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The canonical per-test record skeleton shared by every builder.

    Every field defaults to its null/empty form; each builder overrides only
    the ones its record populates. Centralizing the key structure here keeps
    the builders from drifting (the key set is asserted identical in tests).
    """
    return {
        "test_id": test.id,
        "suite": test.suite,
        "type": "execution",
        "category": test.category,
        "difficulty": test.difficulty,
        "metadata": getattr(test, "metadata", None) or {},
        "mode": mode,
        "prompt_hash": None,
        "model": _model_info(model_config),
        "variant": variant if variant is not None else _baseline_variant_info(),
        "output": {"raw_completion": None, "code": None},
        "scores": _null_scores(),
        "grader": None,
        "usage": _empty_usage(),
        "model_call": None,
        "error": None,
    }


def build_execution_record(
    *,
    test: Any,
    mode: str,
    model_config: ModelConfig | None,
    prompt_hash: str | None,
    raw_completion: str | None,
    code: str,
    env_result: Any,
    scores: dict[str, Any],
    usage: dict[str, Any] | None = None,
    model_call: dict[str, Any] | None = None,
    variant: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical record for an execution test result.

    Args:
        scores: Scores object from BenchmarkRunner._score_execution, carrying
            correctness (legacy), execution_pass (primary), runtime, static,
            and static_policy_pass.
        variant: Variant identity from skills.Variant.record_info();
            defaults to the baseline (no-skills) variant.
    """
    raw = env_result.raw or {}
    record = _base_record(test=test, mode=mode, model_config=model_config, variant=variant)
    record["prompt_hash"] = prompt_hash
    record["output"] = {"raw_completion": raw_completion, "code": code}
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
    mode: str,
    model_config: ModelConfig | None,
    error_type: str,
    error_message: str,
    variant: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical record for a test that errored before grading.

    Used by ``run.continue_on_error``: the record fills the reserved
    ``error`` field, carries null scores (an ungraded test has no score,
    pass or fail), and keeps the exact canonical key structure so consumers
    never need a separate parser for errored tests.
    """
    record = _base_record(test=test, mode=mode, model_config=model_config, variant=variant)
    record["error"] = {"type": error_type, "message": error_message}
    return record


def build_exploit_audit_record(
    *,
    test: Any,
    candidates_tried: int,
    passing_exploit: str | None,
    exploit_code: str | None,
) -> dict[str, Any]:
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


def execution_record_passed(record: dict[str, Any]) -> bool:
    """Whether an execution record represents a strict pass.

    Reads the primary execution_pass metric. Used by reference-solution
    mode to decide failures.
    """
    return bool((record.get("scores") or {}).get("execution_pass"))


def sort_records(records: list) -> list:
    """Order records deterministically for stable output diffs."""
    return sorted(records, key=lambda record: record.get("test_id", ""))


def errored_test_ids(records: list) -> list:
    """Sorted ids of records that errored (run.continue_on_error)."""
    return sorted(record["test_id"] for record in records if record.get("error") is not None)
