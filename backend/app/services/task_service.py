"""
Task registration — validate a stored task directory and record it.

This logic used to live inside ingest.py as private functions, which meant
datasets.py imported `_persist_task`, `_gather_files`, and `_default_task_toml`
*from another router*. A dataset upload is not an HTTP concern of the ingest
endpoint; it just needed the same work done. Now both routers call a service,
and refactoring ingest.py can't silently break dataset upload.

Nothing here touches HTTP. Registration failures raise TaskParseError, which the
routers translate to a 422 — so the same functions are usable from the CLI, a
migration script, or a test without a request object.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.services.task_parser import TaskParseError, parse_task_toml
from app.services.task_validator import ValidationResult, validate_task

# Bundled example evaluations live at the repo root (…/tracetensor/examples).
EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"


def gather_files(src_dir: Path) -> dict[str, bytes]:
    """Read an evaluation directory into {relative_path: bytes}."""
    out: dict[str, bytes] = {}
    for f in src_dir.rglob("*"):
        if f.is_file():
            out[str(f.relative_to(src_dir))] = f.read_bytes()
    return out


def default_task_toml(name: str) -> bytes:
    """A minimal, valid task.toml for a directory that shipped without one.

    Deliberately conservative: `network_mode = "public"` matches what an author
    who never thought about the sandbox would expect, and the timeouts are the
    same defaults the parser applies.
    """
    return (
        'schema_version = "1.3"\n\n'
        "[task]\n"
        f'name = "{name}"\n\n'
        "[verifier]\ntimeout_sec = 120.0\n\n"
        "[agent]\ntimeout_sec = 120.0\n\n"
        '[environment]\nnetwork_mode = "public"\nbuild_timeout_sec = 600.0\n'
    ).encode()


async def persist_task(db: AsyncSession, task_dir: Path) -> tuple[Task, ValidationResult]:
    """Validate a stored evaluation dir and write/update its DB record.

    Supports both native task.toml format and HUD-format (env.py).

    Upserts on the task's declared name, so re-uploading a task updates it in
    place rather than accumulating duplicates. Raises TaskParseError if
    task.toml can't be parsed — the caller decides what that means over HTTP.

    Returns the row and its validation result together. The validation isn't
    stored on the row (it's recomputed from disk each time), and it used to be
    smuggled back on a `task._validation` attribute; an explicit tuple means a
    caller can't forget it exists.
    """
    if (task_dir / "env.py").exists() and not (task_dir / "task.toml").exists():
        return await _persist_hud_task(db, task_dir)

    validation = validate_task(task_dir)
    cfg = parse_task_toml((task_dir / "task.toml").read_bytes(), task_dir)

    existing = (await db.execute(select(Task).where(Task.name == cfg.name))).scalar_one_or_none()

    env_image = cfg.environment.docker_image or (
        "Dockerfile" if validation.has_dockerfile else None
    )

    values = dict(
        name=cfg.name,
        description=cfg.description,
        task_type="coding",
        schema_version=cfg.schema_version,
        config=cfg.model_dump(),
        task_dir=str(task_dir),
        has_instruction=validation.has_instruction,
        has_dockerfile=validation.has_dockerfile,
        has_docker_image=validation.has_docker_image,
        has_test_script=validation.has_test_script,
        has_solution=validation.has_solution,
        environment_image=env_image,
        environment_os=cfg.environment.os,
        network_mode=cfg.environment.network_mode,
        timeout_agent_sec=cfg.agent.timeout_sec or 120.0,
        timeout_verifier_sec=cfg.verifier.timeout_sec,
        category=cfg.category,
        status=validation.status,
    )

    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        task = existing
    else:
        task = Task(id=uuid.uuid4(), **values)
        db.add(task)

    await db.commit()
    await db.refresh(task)
    return task, validation


async def _persist_hud_task(db: AsyncSession, task_dir: Path) -> tuple[Task, ValidationResult]:
    """Register a HUD-format task (env.py + tasks.py) without a task.toml."""
    from app.services.hud_adapter import HudLoadError, load_hud_env

    validation = validate_task(task_dir)

    try:
        loaded = load_hud_env(task_dir)
    except HudLoadError as e:
        raise TaskParseError(f"Could not load env.py: {e}") from e

    name = loaded.env.name
    task_count = len(loaded.tasks)

    existing = (await db.execute(select(Task).where(Task.name == name))).scalar_one_or_none()

    values = dict(
        name=name,
        description=f"HUD-format environment '{name}' ({task_count} task(s)).",
        task_type="hud",
        schema_version="hud/1.0",
        config={"format": "hud", "task_count": task_count},
        task_dir=str(task_dir),
        has_instruction=True,
        has_dockerfile=False,
        has_docker_image=False,
        has_test_script=True,
        has_solution=False,
        environment_image=None,
        environment_os=None,
        network_mode="public",
        timeout_agent_sec=120.0,
        timeout_verifier_sec=120.0,
        category=None,
        status=validation.status,
    )

    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        task = existing
    else:
        task = Task(id=uuid.uuid4(), **values)
        db.add(task)

    await db.commit()
    await db.refresh(task)
    return task, validation


__all__ = [
    "EXAMPLES_DIR",
    "TaskParseError",
    "default_task_toml",
    "gather_files",
    "persist_task",
]
