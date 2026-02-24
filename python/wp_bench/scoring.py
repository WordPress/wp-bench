"""Score aggregation utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Dict, List, Optional


@dataclass
class ScoreBreakdown:
    knowledge: Optional[float] = None
    correctness: Optional[float] = None
    quality: Optional[float] = None
    weights: Dict[str, float] = field(
        default_factory=lambda: {"knowledge": 0.3, "correctness": 0.4, "quality": 0.3}
    )

    def overall(self) -> float:
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
        self.quality_scores: List[float] = []

    def add_execution(self, correctness: float, quality: float | None = None) -> None:
        self.correctness_scores.append(correctness)
        if quality is not None:
            self.quality_scores.append(quality)

    def add_knowledge(self, score: float) -> None:
        self.knowledge_scores.append(score)

    def finalize(self) -> ScoreBreakdown:
        breakdown = ScoreBreakdown()
        if self.knowledge_scores:
            breakdown.knowledge = mean(self.knowledge_scores)
        if self.correctness_scores:
            breakdown.correctness = mean(self.correctness_scores)
        if self.quality_scores:
            breakdown.quality = mean(self.quality_scores)
        return breakdown
