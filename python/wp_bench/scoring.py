"""Score aggregation utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Dict, List


@dataclass
class ScoreBreakdown:
    knowledge: float = 0.0
    correctness: float = 0.0
    quality: float = 0.0
    weights: Dict[str, float] = field(
        default_factory=lambda: {"knowledge": 0.3, "correctness": 0.4, "quality": 0.3}
    )

    def overall(self) -> float:
        total = 0.0
        for key, weight in self.weights.items():
            total += getattr(self, key, 0.0) * weight
        return round(total, 4)


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
        else:
            breakdown.quality = 0.0
        return breakdown
