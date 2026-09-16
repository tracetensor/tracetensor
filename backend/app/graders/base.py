"""
Base types for the grader system.

SubScore    — one grader's result with name + metadata for auditability.
EvaluationResult — top-level result, optionally decomposed into SubScores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubScore:
    """One grader's contribution to a composite score."""
    name: str
    score: float
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __float__(self) -> float:
        return float(self.score)

    def __repr__(self) -> str:
        return f"SubScore(name={self.name!r}, score={self.score:.3f}, weight={self.weight})"


@dataclass
class EvaluationResult:
    """Final score for a graded step, optionally broken down into subscores."""
    score: float
    reason: str = ""
    subscores: list[SubScore] = field(default_factory=list)

    def __float__(self) -> float:
        return float(self.score)

    def __repr__(self) -> str:
        return f"EvaluationResult(score={self.score:.3f}, reason={self.reason!r})"

    def clamp(self) -> "EvaluationResult":
        """Return a copy with score clamped to [0.0, 1.0]."""
        return EvaluationResult(
            score=max(0.0, min(1.0, self.score)),
            reason=self.reason,
            subscores=self.subscores,
        )
