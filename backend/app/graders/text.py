"""
Text graders — all normalize before comparing.

Normalization pipeline (applied to both prediction and ground truth):
  1. Lowercase
  2. Strip punctuation
  3. Remove leading articles (a, an, the)
  4. Collapse whitespace

All functions are synchronous and return EvaluationResult.
"""

from __future__ import annotations

import re
import string
from typing import Sequence

from app.graders.base import EvaluationResult

_PUNCT = str.maketrans("", "", string.punctuation)
_ARTICLE_RE = re.compile(r"\b(a|an|the)\b")


def _norm(text: str) -> str:
    text = text.lower()
    text = text.translate(_PUNCT)
    text = _ARTICLE_RE.sub(" ", text)
    return " ".join(text.split())


# ── Exact match ───────────────────────────────────────────────────────────────


def exact_match(prediction: str, ground_truth: str) -> EvaluationResult:
    """1.0 if normalized strings match exactly, else 0.0."""
    p, g = _norm(prediction), _norm(ground_truth)
    score = 1.0 if p == g else 0.0
    return EvaluationResult(
        score=score,
        reason=f"exact_match: {p!r} {'==' if score else '!='} {g!r}",
    )


# ── Substring / containment ───────────────────────────────────────────────────


def contains(prediction: str, substring: str) -> EvaluationResult:
    """1.0 if prediction contains substring (after normalization)."""
    p, s = _norm(prediction), _norm(substring)
    score = 1.0 if s in p else 0.0
    return EvaluationResult(
        score=score,
        reason=f"contains: {s!r} {'in' if score else 'not in'} prediction",
    )


def contains_any(prediction: str, substrings: Sequence[str]) -> EvaluationResult:
    """1.0 if prediction contains ANY of the substrings (logical OR)."""
    p = _norm(prediction)
    matched = [s for s in substrings if _norm(s) in p]
    score = 1.0 if matched else 0.0
    return EvaluationResult(
        score=score,
        reason=f"contains_any: matched={matched!r} of {list(substrings)!r}",
    )


def contains_all(prediction: str, substrings: Sequence[str]) -> EvaluationResult:
    """1.0 if prediction contains ALL of the substrings (logical AND)."""
    p = _norm(prediction)
    missing = [s for s in substrings if _norm(s) not in p]
    score = 1.0 if not missing else 0.0
    return EvaluationResult(
        score=score,
        reason=f"contains_all: missing={missing!r} of {list(substrings)!r}",
    )


# ── Numeric ───────────────────────────────────────────────────────────────────


def numeric_match(
    prediction: str,
    expected: float | int | str,
    *,
    tolerance: float = 0.0,
    relative: bool = False,
) -> EvaluationResult:
    """Score based on numeric proximity.

    tolerance=0.0 (default): exact numeric equality after parsing.
    tolerance>0, relative=False: absolute |pred - expected| <= tolerance.
    tolerance>0, relative=True: |pred - expected| / |expected| <= tolerance.

    Returns 0.0 if the prediction cannot be parsed as a number.
    """
    try:
        pred_val = float(re.sub(r"[^\d.\-+eE]", "", prediction.strip()))
    except ValueError:
        return EvaluationResult(
            score=0.0,
            reason=f"numeric_match: could not parse {prediction!r} as number",
        )

    exp_val = float(expected)

    if tolerance == 0.0:
        score = 1.0 if pred_val == exp_val else 0.0
        reason = f"numeric_match: {pred_val} {'==' if score else '!='} {exp_val}"
    elif relative:
        denom = abs(exp_val) if exp_val != 0 else 1.0
        err = abs(pred_val - exp_val) / denom
        score = 1.0 if err <= tolerance else 0.0
        reason = f"numeric_match: rel_err={err:.4f} vs tolerance={tolerance}"
    else:
        err = abs(pred_val - exp_val)
        score = 1.0 if err <= tolerance else 0.0
        reason = f"numeric_match: abs_err={err} vs tolerance={tolerance}"

    return EvaluationResult(score=score, reason=reason)


# ── F1 ────────────────────────────────────────────────────────────────────────


def f1_score(prediction: str, ground_truth: str) -> EvaluationResult:
    """Token-level F1 between normalized prediction and ground truth.

    Useful when the answer is a phrase and partial credit makes sense.
    Returns 1.0 if both strings normalize to the same tokens, 0.0 if one
    is empty, and interpolated precision×recall otherwise.
    """
    pred_tokens = _norm(prediction).split()
    true_tokens = _norm(ground_truth).split()

    if not pred_tokens and not true_tokens:
        return EvaluationResult(score=1.0, reason="f1: both empty after normalization")
    if not pred_tokens or not true_tokens:
        return EvaluationResult(score=0.0, reason="f1: one side is empty")

    pred_set = set(pred_tokens)
    true_set = set(true_tokens)
    common = pred_set & true_set

    precision = len(common) / len(pred_set)
    recall = len(common) / len(true_set)

    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return EvaluationResult(
        score=f1,
        reason=f"f1: precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}",
    )
