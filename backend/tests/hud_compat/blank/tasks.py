"""Concrete tasks for the blank environment — mirrors HUD's blank template."""

from env import count, env

tasks = [
    count(sentence="Strawberry world", letter="r"),
    count(sentence="banana", letter="a"),
    count(sentence="hello world", letter="l"),
]

__all__ = ["env", "tasks"]
