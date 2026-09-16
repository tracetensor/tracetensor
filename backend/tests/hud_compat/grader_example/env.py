"""
Grader system example env.py — exercises all four high-leverage graders.

Templates:
  capital_city   — exact_match + f1_score via combine (text graders)
  unit_convert   — numeric_match with tolerance
  bash_exit      — BashGrader: runs `echo ok && exit 0`
  multi_contain  — contains_any / contains_all
"""

from __future__ import annotations

from hud import Environment
from hud.graders import (
    BashGrader,
    combine,
    combine_all,
    combine_any,
    contains,
    contains_all,
    contains_any,
    exact_match,
    f1_score,
    numeric_match,
)

env = Environment(name="grader-example")


@env.template(id="capital_city")
async def capital_city(country: str, capital: str):
    """Agent must name the capital city. Scored by exact_match + f1_score."""
    answer = yield f"What is the capital city of {country}?"
    em = exact_match(answer or "", capital)
    f1 = f1_score(answer or "", capital)
    result = await combine(em, f1, weights=[0.7, 0.3], names=["exact", "f1"])
    yield float(result)


@env.template(id="unit_convert")
async def unit_convert(value: float, from_unit: str, to_unit: str, expected: float, tol: float):
    """Agent must convert a unit. Scored by numeric_match with tolerance."""
    answer = yield f"Convert {value} {from_unit} to {to_unit}. Answer with only the number."
    result = numeric_match(answer or "", expected, tolerance=tol)
    yield float(result)


@env.template(id="bash_exit")
async def bash_exit():
    """BashGrader: run a shell command that exits 0."""
    answer = yield "Say 'ready' when you're done."
    result = await BashGrader.grade(command="echo ok && exit 0", timeout=10)
    yield float(result)


@env.template(id="multi_contain")
async def multi_contain(required: list, any_of: list):
    """contains_all + contains_any composition via combine_all."""
    answer = yield (
        f"Write a sentence that includes ALL of: {required} "
        f"and at least ONE of: {any_of}."
    )
    all_result = contains_all(answer or "", required)
    any_result = contains_any(answer or "", any_of)
    result = await combine_all(all_result, any_result, names=["must_have_all", "must_have_any"])
    yield float(result)
