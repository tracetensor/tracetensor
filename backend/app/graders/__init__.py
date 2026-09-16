"""
TraceTensor grader system.

Graders score agent output on a 0.0–1.0 scale. Each grader is either:
  - A plain async function returning float or SubScore
  - A class with a .grade() classmethod

Composability:
  combine()     — weighted parallel composition (positive weights sum to 1.0;
                  negative weights are penalties subtracted from the total)
  combine_any() — max of scores (logical OR)
  combine_all() — min of scores (logical AND)

Text graders (all normalize: lowercase, strip punctuation/articles):
  exact_match, contains, contains_any, contains_all, numeric_match, f1_score

Shell grader:
  BashGrader — runs a shell command, scores on exit code, kills the
               process group on timeout

Import from the stub module (hud.graders) or directly from this package.
"""

from app.graders.base import EvaluationResult, SubScore
from app.graders.combine import combine, combine_all, combine_any
from app.graders.text import (
    contains,
    contains_all,
    contains_any,
    exact_match,
    f1_score,
    numeric_match,
)
from app.graders.bash import BashGrader
from app.graders.llm_judge import LLMJudgeGrader

__all__ = [
    "EvaluationResult",
    "SubScore",
    "combine",
    "combine_any",
    "combine_all",
    "exact_match",
    "contains",
    "contains_any",
    "contains_all",
    "numeric_match",
    "f1_score",
    "BashGrader",
    "LLMJudgeGrader",
]
