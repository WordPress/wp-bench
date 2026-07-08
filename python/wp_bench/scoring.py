"""Score aggregation utilities.

Scoring model (SCORING_VERSION 2.0): runtime behavior is the primary
execution signal. A task passes strictly (``execution_pass``) when the
code runs without crash/timeout, passes its runtime assertions, and
triggers no forbidden static pattern with severity ``error``. Static
required-pattern scores are diagnostics; they no longer grant or deny
correctness credit on their own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Dict, List, Optional

#: Bump when the meaning of any aggregate or per-test score changes.
SCORING_VERSION = "2.0"


@dataclass
class ScoreBreakdown:
    knowledge: Optional[float] = None
    #: Legacy compatibility score (see records: 1.0 on strict pass, else
    #: partial runtime credit). Kept one release for consumers of the old key.
    correctness: Optional[float] = None
    #: Strict pass rate: fraction of execution tests with execution_pass=True.
    #: This is the primary execution ranking metric.
    execution_pass_rate: Optional[float] = None
    #: Mean partial runtime assertion score (0.0-1.0).
    runtime: Optional[float] = None
    #: Fraction of execution tests without a hard static policy failure.
    static_policy_pass_rate: Optional[float] = None
    weights: Dict[str, float] = field(
        default_factory=lambda: {"knowledge": 0.3, "execution_pass_rate": 0.7}
    )

    def overall(self) -> float:
        """Weighted overall score (formula versioned by SCORING_VERSION).

        v2.0: 0.3 * knowledge + 0.7 * strict execution pass rate, over the
        dimensions actually present. Prefer the separate metrics for any
        official comparison; overall is a convenience summary only.
        """
        active = {k: w for k, w in self.weights.items() if getattr(self, k) is not None}
        if not active:
            return 0.0
        total_weight = sum(active.values())
        total = sum(getattr(self, k) * w for k, w in active.items())
        return round(total / total_weight, 4)


class ScoreAggregator:
    def __init__(self) -> None:
        self.knowledge_scores: List[float] = []
        self.correctness_scores: List[float] = []
        self.execution_passes: List[bool] = []
        self.runtime_scores: List[float] = []
        self.static_policy_passes: List[bool] = []

    def add_execution(self, scores: Dict[str, Any]) -> None:
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

    def add_knowledge(self, score: float) -> None:
        self.knowledge_scores.append(score)

    def finalize(self) -> ScoreBreakdown:
        breakdown = ScoreBreakdown()
        if self.knowledge_scores:
            breakdown.knowledge = mean(self.knowledge_scores)
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
