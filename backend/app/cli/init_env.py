"""
`tracetensor init` — scaffold a new HUD-style evaluation environment.

    tracetensor init my-env
    tracetensor init my-env --template blank
    tracetensor init my-env --template counter

Creates env.py, tasks.py, Dockerfile.hud, pyproject.toml ready to run with:
    tracetensor run my-env -a openai
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from app.cli import console as ui

# ── Templates ─────────────────────────────────────────────────────────────────

_BLANK_ENV = '''\
"""
{name} — evaluation environment.

Add @env.template functions below. Each one is a task:
  - first yield  → the prompt sent to the agent
  - second yield → the score (0.0 .. 1.0) after grading the agent's answer
"""

from hud import Environment
from hud.graders import combine, exact_match, f1_score, contains

env = Environment(name="{name}")


@env.template(id="example")
async def example(question: str, expected: str):
    """Simple Q&A task graded by exact match + partial F1 credit."""
    answer = yield question
    em = exact_match(answer or "", expected)
    f1 = f1_score(answer or "", expected)
    score = await combine(em, f1, weights=[0.7, 0.3], names=["exact", "f1"])
    yield float(score)
'''

_BLANK_TASKS = '''\
"""Concrete tasks for {name}."""

from env import example

tasks = [
    example(question="What is the capital city of France?", expected="Paris"),
    example(question="What is the capital city of Japan?",  expected="Tokyo"),
]
'''

_BLANK_DOCKERFILE = '''\
# Optional: only needed for workspace / shell tasks.
# For text-only graded tasks you can delete this file.
FROM python:3.11-slim
WORKDIR /workspace
RUN pip install --no-cache-dir pytest
'''

_BLANK_PYPROJECT = '''\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "0.1.0"
description = "TraceTensor evaluation environment"
requires-python = ">=3.10"
dependencies = []
'''

_COUNTER_ENV = '''\
"""
counter — letter-counting evaluation environment.

Demonstrates:
  env.workspace()   → agent gets a sandboxed shell + files
  @env.initialize   → seed a file before the agent connects
  @env.template     → parametrised task recipe
"""

from pathlib import Path
from hud import Environment
from hud.graders import contains

ROOT = Path("workspace")
env = Environment(name="counter")
env.workspace(ROOT)


@env.initialize
async def _setup():
    (ROOT / "instructions.txt").write_text(
        "Count letters case-insensitively.\\n"
    )


@env.shutdown
async def _teardown():
    (ROOT / "instructions.txt").unlink(missing_ok=True)


@env.template(id="count")
async def count(sentence: str, letter: str):
    """How many times does a letter appear in a sentence?"""
    answer = yield (
        f"How many times does \\'{letter}\\' appear in \\'{sentence}\\'? "
        "Answer with only the number."
    )
    expected = str(sentence.lower().count(letter.lower()))
    yield 1.0 if expected in (answer or "") else 0.0
'''

_COUNTER_TASKS = '''\
"""Concrete tasks for the counter environment."""

from env import count

tasks = [
    count(sentence="strawberry", letter="r"),
    count(sentence="banana",     letter="a"),
    count(sentence="Mississippi", letter="s"),
    count(sentence="hello world", letter="l"),
]
'''

_TEMPLATES = {
    "blank":   (_BLANK_ENV,   _BLANK_TASKS,   _BLANK_DOCKERFILE,   _BLANK_PYPROJECT),
    "counter": (_COUNTER_ENV, _COUNTER_TASKS, _BLANK_DOCKERFILE,   _BLANK_PYPROJECT),
}


def init_env_cmd(
    name: str = typer.Argument(..., help="Directory name (also becomes the env name)."),
    template: str = typer.Option(
        "blank", "--template", "-t",
        help=f"Starter template: {', '.join(_TEMPLATES)}",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    output: Optional[Path] = typer.Option(
        None, "-o", "--output", help="Parent directory (default: current dir)."
    ),
) -> None:
    """Scaffold a new evaluation environment (HUD-compatible format).

    Creates env.py, tasks.py, Dockerfile.hud, and pyproject.toml ready to run:

        tracetensor run my-env -a openai
    """
    if template not in _TEMPLATES:
        ui.error(f"Unknown template {template!r}. Choose from: {', '.join(_TEMPLATES)}")
        raise typer.Exit(2)

    base = (output or Path()).resolve()
    dest = base / name

    if dest.exists() and any(dest.iterdir()) and not force:
        ui.error(
            f"{dest} already exists and is not empty. "
            "Use --force to overwrite scaffold files."
        )
        raise typer.Exit(2)

    dest.mkdir(parents=True, exist_ok=True)

    env_py, tasks_py, dockerfile, pyproject = _TEMPLATES[template]
    files = {
        "env.py":         env_py.format(name=name),
        "tasks.py":       tasks_py.format(name=name),
        "Dockerfile.hud": dockerfile,
        "pyproject.toml": pyproject.format(name=name),
    }
    for filename, content in files.items():
        fpath = dest / filename
        if fpath.exists() and not force:
            continue
        fpath.write_text(content, encoding="utf-8")

    ui.banner()
    ui.console.print(f"\n[ok]✓ scaffolded[/] [val]{dest}[/]  (template: {template})\n")
    ui.console.print("  Files created:")
    for f in files:
        ui.console.print(f"    [muted]{dest / f}[/]")
    ui.console.print()
    ui.hint(f"Run against one model:    tracetensor run {dest} -a openai")
    ui.hint(f"Compare multiple models:  tracetensor run {dest} --models gpt-4o-mini,gpt-4.1-mini")
    ui.hint(f"Validate structure:       tracetensor tasks validate {dest}")
    ui.console.print()
