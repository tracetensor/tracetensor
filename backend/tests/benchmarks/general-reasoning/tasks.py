"""
Concrete benchmark tasks — 10 tasks across 5 grading families.

These test different reasoning skills:
  - Geographic knowledge (capital cities)
  - Unit conversion math
  - List recall
  - Explanation quality (LLM judge)
  - Composite scoring
"""

from env import capital_city, unit_convert, list_task, explain, composite

tasks = [
    # ── Capital cities ────────────────────────────────────────────────────────
    capital_city(country="France",  capital="Paris"),
    capital_city(country="Japan",   capital="Tokyo"),
    capital_city(country="Brazil",  capital="Brasília"),

    # ── Unit conversions ──────────────────────────────────────────────────────
    unit_convert(value=100,  from_unit="Celsius",    to_unit="Fahrenheit", expected=212.0,  tol=0.5),
    unit_convert(value=1,    from_unit="kilometer",  to_unit="meters",     expected=1000.0, tol=1.0),
    unit_convert(value=2.54, from_unit="centimeters",to_unit="inches",     expected=1.0,    tol=0.05),

    # ── List recall ───────────────────────────────────────────────────────────
    list_task(
        prompt="List three primary colors. Include all three in your answer.",
        required_items=["red", "blue", "yellow"],
    ),

    # ── Open-ended explanation (LLM judge) ────────────────────────────────────
    explain(
        concept="how a neural network learns",
        criteria=[
            {"criterion": "Mentions weights or parameters being updated", "weight": 0.4},
            {"criterion": "Mentions loss or error or gradient",           "weight": 0.4},
            {"criterion": "The explanation is 2-3 sentences long",        "weight": 0.2},
        ],
    ),

    # ── Composite: word + number ──────────────────────────────────────────────
    # The composite grader checks: does the answer contain "seven" AND is there
    # a number within 2 of 7? "seven 7" should score 0.5 (word match) + 0.5 (num match).
    # We ask for just the number so the numeric grader fires cleanly.
    composite(
        question="How many days are in a week? Write the English word for it, then the digit.",
        expected_word="seven",
        expected_number=7,
    ),

    # ── Bonus: tricky capital ─────────────────────────────────────────────────
    capital_city(country="Australia", capital="Canberra"),
]

__all__ = ["tasks"]
