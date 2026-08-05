"""Score aggregation utilities.

Scoring model (SCORING_VERSION 3.0): the knowledge track was removed, so
the benchmark is execution-only and ``overall`` equals the strict
execution pass rate. Runtime behavior is the primary execution signal. A
task passes strictly (``execution_pass``) when the code runs without
crash/timeout, passes its runtime assertions, and triggers no forbidden
static pattern with severity ``error``. Static required-pattern scores
are diagnostics; they no longer grant or deny correctness credit on
their own.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any

#: Bump when the meaning of any aggregate or per-test score changes.
#: 3.0: knowledge track removed; overall is execution-only.
SCORING_VERSION = "3.0"


@dataclass
class ScoreBreakdown:
    #: Legacy compatibility score (see records: 1.0 on strict pass, else
    #: partial runtime credit). Kept one release for consumers of the old key.
    correctness: float | None = None
    #: Strict pass rate: fraction of execution tests with execution_pass=True.
    #: This is the primary execution ranking metric.
    execution_pass_rate: float | None = None
    #: Mean partial runtime assertion score (0.0-1.0).
    runtime: float | None = None
    #: Fraction of execution tests without a hard static policy failure.
    static_policy_pass_rate: float | None = None

    def as_scores_dict(self) -> dict[str, Any]:
        """The summary scores object recorded in payload metadata.

        Versioned alongside SCORING_VERSION so the key set lives next to
        the formula it summarizes.
        """
        return {
            "correctness": self.correctness,
            "execution_pass_rate": self.execution_pass_rate,
            "runtime": self.runtime,
            "static_policy_pass_rate": self.static_policy_pass_rate,
            "overall": self.overall(),
        }

    def overall(self) -> float:
        """Overall score (formula versioned by SCORING_VERSION).

        v3.0: the strict execution pass rate (the knowledge track was
        removed). Prefer the separate metrics for any official comparison;
        overall is a convenience summary only.
        """
        if self.execution_pass_rate is None:
            return 0.0
        return round(self.execution_pass_rate, 4)


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; values need not be pre-sorted."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class UsageAggregator:
    """Accumulates per-call usage into a run-level summary."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cost_usd = 0.0
        self.has_cost = False
        self.latencies_ms: list[float] = []

    def add(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        for token_field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(token_field)
            if isinstance(value, (int, float)):
                setattr(self, token_field, getattr(self, token_field) + int(value))
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)):
            self.cost_usd += float(cost)
            self.has_cost = True
        latency = usage.get("latency_ms")
        if isinstance(latency, (int, float)):
            self.latencies_ms.append(float(latency))

    def summary(self) -> dict[str, Any]:
        """Run-level usage summary; cost is an estimate, not billing truth."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.cost_usd, 6) if self.has_cost else None,
            "median_latency_ms": (
                round(_percentile(self.latencies_ms, 0.5), 1) if self.latencies_ms else None
            ),
            "p95_latency_ms": (
                round(_percentile(self.latencies_ms, 0.95), 1) if self.latencies_ms else None
            ),
        }


class ScoreAggregator:
    def __init__(self) -> None:
        self.correctness_scores: list[float] = []
        self.execution_passes: list[bool] = []
        self.runtime_scores: list[float] = []
        self.static_policy_passes: list[bool] = []

    def add_execution(self, scores: dict[str, Any]) -> None:
        """Record an execution test's scores object (canonical record shape)."""
        correctness = scores.get("correctness")
        if isinstance(correctness, (int, float)):
            self.correctness_scores.append(float(correctness))
        self.execution_passes.append(bool(scores.get("execution_pass")))
        runtime = scores.get("runtime")
        if isinstance(runtime, (int, float)):
            self.runtime_scores.append(float(runtime))
        policy = scores.get("static_policy_pass")
        if policy is not None:
            self.static_policy_passes.append(bool(policy))

    def finalize(self) -> ScoreBreakdown:
        breakdown = ScoreBreakdown()
        if self.correctness_scores:
            breakdown.correctness = mean(self.correctness_scores)
        if self.execution_passes:
            breakdown.execution_pass_rate = round(
                sum(self.execution_passes) / len(self.execution_passes), 4
            )
        if self.runtime_scores:
            breakdown.runtime = round(mean(self.runtime_scores), 4)
        if self.static_policy_passes:
            breakdown.static_policy_pass_rate = round(
                sum(self.static_policy_passes) / len(self.static_policy_passes), 4
            )
        return breakdown
