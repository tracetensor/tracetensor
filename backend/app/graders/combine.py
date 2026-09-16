"""
Grader combinators.

combine()     — Weighted composition. Positive weights (default 1.0 each) are
                normalized so they sum to 1.0. Negative weights are penalties:
                the magnitude is multiplied by the grader's score and subtracted
                from the total, then the result is clamped to [0.0, 1.0].

combine_any() — max(scores) — logical OR over a set of graders.
combine_all() — min(scores) — logical AND over a set of graders.

All functions accept either plain floats, EvaluationResult, SubScore, or
awaitables (coroutines / tasks) whose results are any of the above. Mixing
sync and async inputs is fine.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.graders.base import EvaluationResult, SubScore


def _as_float(result: Any) -> float:
    if isinstance(result, (EvaluationResult, SubScore)):
        return float(result.score)
    try:
        return float(result)
    except (TypeError, ValueError):
        return 0.0


async def _resolve(value: Any) -> Any:
    """Await coroutines; return everything else as-is."""
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


async def combine(
    *graders: Any,
    weights: list[float] | None = None,
    names: list[str] | None = None,
) -> EvaluationResult:
    """Run graders concurrently, combine with optional weights.

    Positive weights are normalized so they sum to 1.0.
    Negative weights are applied as penalties (subtracted).
    Result is clamped to [0.0, 1.0].

    Example::

        result = await combine(
            exact_match(answer, "Paris"),
            contains(answer, "capital"),
            weights=[0.8, 0.2],
        )
    """
    raw = await asyncio.gather(*[_resolve(g) for g in graders])
    n = len(raw)
    ws = list(weights) if weights is not None else [1.0] * n
    if len(ws) < n:
        ws += [1.0] * (n - len(ws))
    ns = list(names) if names is not None else [f"grader_{i}" for i in range(n)]
    if len(ns) < n:
        ns += [f"grader_{i}" for i in range(len(ns), n)]

    pos_total = sum(w for w in ws if w > 0)
    if pos_total == 0:
        pos_total = 1.0

    score = 0.0
    subscores: list[SubScore] = []
    for i, (res, w, name) in enumerate(zip(raw, ws, ns)):
        s = _as_float(res)
        meta: dict = {}
        if isinstance(res, EvaluationResult):
            meta["reason"] = res.reason
        subscores.append(SubScore(name=name, score=s, weight=w, metadata=meta))
        if w > 0:
            score += s * (w / pos_total)
        else:
            # Penalty: subtract proportional to how much the bad condition is met
            score -= s * abs(w) / pos_total

    final = max(0.0, min(1.0, score))
    reasons = [f"{ss.name}={ss.score:.2f}" for ss in subscores]
    return EvaluationResult(
        score=final,
        reason="combine: " + ", ".join(reasons),
        subscores=subscores,
    )


async def combine_any(*graders: Any, names: list[str] | None = None) -> EvaluationResult:
    """max(scores) — passes if ANY grader passes (logical OR)."""
    raw = await asyncio.gather(*[_resolve(g) for g in graders])
    scores = [_as_float(r) for r in raw]
    ns = list(names) if names else [f"grader_{i}" for i in range(len(raw))]
    subscores = [SubScore(name=n, score=s) for n, s in zip(ns, scores)]
    best = max(scores, default=0.0)
    return EvaluationResult(
        score=best,
        reason="combine_any: max(" + ", ".join(f"{s:.2f}" for s in scores) + ")",
        subscores=subscores,
    )


async def combine_all(*graders: Any, names: list[str] | None = None) -> EvaluationResult:
    """min(scores) — passes only if ALL graders pass (logical AND)."""
    raw = await asyncio.gather(*[_resolve(g) for g in graders])
    scores = [_as_float(r) for r in raw]
    ns = list(names) if names else [f"grader_{i}" for i in range(len(raw))]
    subscores = [SubScore(name=n, score=s) for n, s in zip(ns, scores)]
    worst = min(scores, default=0.0)
    return EvaluationResult(
        score=worst,
        reason="combine_all: min(" + ", ".join(f"{s:.2f}" for s in scores) + ")",
        subscores=subscores,
    )
