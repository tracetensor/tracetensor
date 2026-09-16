"""
LLMJudgeGrader — score agent output against a weighted criteria list.

Each criterion is evaluated in parallel by a fast model (default: claude-haiku-4-5).
The model responds with {"criterion_status": "MET"} or {"criterion_status": "UNMET"}.
Score = weighted sum of MET criteria / total positive weight, clamped to [0, 1].

Usage:
    result = await LLMJudgeGrader.grade(
        output="The cat sat on the mat.",
        criteria=[
            {"criterion": "Mentions an animal", "weight": 0.6},
            {"criterion": "Is a complete sentence", "weight": 0.4},
        ],
    )
    # result.score: float, result.subscores: one SubScore per criterion
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.graders.base import EvaluationResult, SubScore

_SYSTEM = (
    "You are a strict binary evaluator. "
    "You are given an OUTPUT and a single CRITERION. "
    "Decide whether the OUTPUT satisfies the criterion. "
    'Reply with ONLY valid JSON: {"criterion_status": "MET"} or {"criterion_status": "UNMET"}. '
    "No explanation, no extra keys, no markdown."
)

_MAX_OUTPUT_CHARS = 12_000
_MAX_CRITERION_CHARS = 1_000


def _parse_status(text: str) -> bool | None:
    """Return True for MET, False for UNMET, None if unparseable."""
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        try:
            obj = json.loads(candidate)
            status = obj.get("criterion_status", "").upper()
            if status == "MET":
                return True
            if status == "UNMET":
                return False
        except (ValueError, TypeError):
            pass
    # Fallback: look for the bare keywords
    upper = text.upper()
    if "\"MET\"" in upper or ": MET" in upper:
        return True
    if "\"UNMET\"" in upper or ": UNMET" in upper:
        return False
    return None


async def _evaluate_one(
    criterion: str,
    output: str,
    provider: str,
    model: str,
    loop: asyncio.AbstractEventLoop,
) -> bool | None:
    """Call the LLM for one criterion and return True/False/None."""
    from app.services import llm as _llm

    user = (
        f"OUTPUT:\n{output[:_MAX_OUTPUT_CHARS]}\n\n"
        f"CRITERION:\n{criterion[:_MAX_CRITERION_CHARS]}"
    )
    try:
        result = await loop.run_in_executor(
            None,
            lambda: _llm.call_llm(provider, model, _SYSTEM, user, max_tokens=40),
        )
        return _parse_status(result.text or "")
    except Exception:
        return None


class LLMJudgeGrader:
    """Grade agent output against a weighted list of criteria.

    Each criterion is evaluated by a fast LLM call; results are combined as a
    weighted sum. Negative-weight criteria act as penalties.

    ``criteria`` items can be:
      - str:  {"criterion": str, "weight": 1.0}  (equal weighting)
      - dict: {"criterion": str, "weight": float}
    """

    DEFAULT_MODEL = "claude-haiku-4-5-20251001"
    DEFAULT_PROVIDER = "anthropic"

    @classmethod
    async def grade(
        cls,
        *,
        output: str,
        criteria: list[dict[str, Any] | str],
        model: str | None = None,
        provider: str | None = None,
        weight: float = 1.0,
    ) -> EvaluationResult:
        """Evaluate output against each criterion in parallel.

        Returns EvaluationResult with one SubScore per criterion plus a
        composite score = sum(met_weight) / sum(positive_weight), clamped [0,1].
        """
        from app.services import llm as _llm

        provider = _llm.canonical_provider(provider or cls.DEFAULT_PROVIDER)
        model = model or cls.DEFAULT_MODEL

        # Normalise criteria list
        parsed: list[tuple[str, float]] = []
        for item in criteria:
            if isinstance(item, str):
                parsed.append((item, 1.0))
            else:
                parsed.append((str(item.get("criterion", item.get("text", ""))),
                               float(item.get("weight", 1.0))))

        if not parsed:
            return EvaluationResult(score=0.0, reason="llm_judge: no criteria provided")

        loop = asyncio.get_event_loop()

        # Evaluate all criteria in parallel
        results = await asyncio.gather(
            *[_evaluate_one(crit, output, provider, model, loop) for crit, _ in parsed],
            return_exceptions=True,
        )

        subscores: list[SubScore] = []
        total_positive = sum(w for _, w in parsed if w > 0)
        weighted_score = 0.0

        for (crit, w), met in zip(parsed, results):
            if isinstance(met, Exception) or met is None:
                met_bool = False
                reason = "evaluation failed"
            else:
                met_bool = bool(met)
                reason = "MET" if met_bool else "UNMET"

            criterion_score = 1.0 if met_bool else 0.0
            subscores.append(SubScore(
                name=crit[:60],
                score=criterion_score,
                weight=w,
                metadata={"criterion": crit, "status": reason, "weight": w},
            ))
            if w > 0:
                weighted_score += criterion_score * w
            else:
                # Negative weight: subtract if the undesired thing is present
                weighted_score += criterion_score * w

        if total_positive > 0:
            score = weighted_score / total_positive
        else:
            score = 0.0

        score = max(0.0, min(1.0, score))
        met_count = sum(1 for ss in subscores if ss.score > 0)
        reason_str = (
            f"llm_judge: {met_count}/{len(subscores)} criteria MET "
            f"via {provider}/{model}"
        )
        return EvaluationResult(score=score, reason=reason_str, subscores=subscores)


__all__ = ["LLMJudgeGrader"]
