"""Concrete tasks for the grader example environment."""

from env import (
    bash_exit,
    capital_city,
    env,
    multi_contain,
    unit_convert,
)

tasks = [
    capital_city(country="France", capital="Paris"),
    capital_city(country="Japan", capital="Tokyo"),
    unit_convert(value=100.0, from_unit="Celsius", to_unit="Fahrenheit",
                 expected=212.0, tol=0.1),
    bash_exit(),
    multi_contain(required=["cat", "dog"], any_of=["happy", "sad", "excited"]),
]

__all__ = ["env", "tasks"]
