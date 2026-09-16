"""
general-reasoning — benchmark environment for comparing LLM models.

Covers 5 grading families:
  1. exact_match + f1      → capital cities, history facts
  2. numeric_match tol     → unit conversion, estimation
  3. contains_all          → list generation
  4. LLMJudgeGrader        → open-ended explanation quality
  5. combine (weighted)    → multi-criterion composite

Run against multiple models:
    tracetensor run . --models gpt-4o-mini,gpt-4.1-mini,gpt-4o -a openai
"""

from __future__ import annotations
from hud import Environment
from hud.graders import (
    LLMJudgeGrader,
    combine,
    contains,
    contains_all,
    exact_match,
    f1_score,
    numeric_match,
)

env = Environment(name="general-reasoning")


# ── 1. Capital cities — exact match + partial f1 credit ─────────────────────

@env.template(id="capital_city")
async def capital_city(country: str, capital: str):
    """Name the capital city of a given country."""
    answer = yield f"What is the capital city of {country}? Reply with only the city name."
    em = exact_match(answer or "", capital)
    f1 = f1_score(answer or "", capital)
    score = await combine(em, f1, weights=[0.8, 0.2], names=["exact", "f1"])
    yield float(score)


# ── 2. Unit conversion — numeric with tolerance ──────────────────────────────

@env.template(id="unit_convert")
async def unit_convert(value: float, from_unit: str, to_unit: str,
                        expected: float, tol: float):
    """Convert a measurement between units. Answer with only the number."""
    answer = yield (
        f"Convert {value} {from_unit} to {to_unit}. "
        "Answer with only the numeric result, no units."
    )
    result = numeric_match(answer or "", expected, tolerance=tol)
    yield float(result)


# ── 3. List generation — contains_all ───────────────────────────────────────

@env.template(id="list_task")
async def list_task(prompt: str, required_items: list):
    """Agent must mention all required items in its answer."""
    answer = yield prompt
    result = contains_all(answer or "", required_items)
    yield float(result)


# ── 4. Open-ended explanation — LLMJudgeGrader ──────────────────────────────

@env.template(id="explain")
async def explain(concept: str, criteria: list):
    """Agent explains a concept; graded by criteria list via LLM judge."""
    answer = yield f"Explain {concept} in 2-3 sentences."
    result = await LLMJudgeGrader.grade(
        output=answer or "",
        criteria=criteria,
        provider="openai",
        model="gpt-4o-mini",
    )
    yield float(result)


# ── 5. Multi-criterion composite ─────────────────────────────────────────────

@env.template(id="composite")
async def composite(question: str, expected_word: str, expected_number: float):
    """Answer must CONTAIN expected word AND a number close to expected."""
    answer = yield question
    word_score = contains(answer or "", expected_word)
    num_score  = numeric_match(answer or "", expected_number, tolerance=2.0)
    score = await combine(
        word_score, num_score,
        weights=[0.5, 0.5],
        names=["word_contains", "numeric_match"],
    )
    yield float(score)
