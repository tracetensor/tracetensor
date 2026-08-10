"""
Enqueueing work — creating a job, or a dataset run and its child jobs.

This lived inline in two route handlers. Each one did the same five things
(idempotency pre-check, agent resolution, row creation, the concurrent-retry
race, event-bus registration) in its own slightly different way, and none of it
could be called from anywhere but an HTTP request — not the CLI, not a cron, not
a test.

The idempotency dance is the part worth understanding, and the reason this is a
service rather than four lines in a router. A retried POST must never start a
second run, because a second run spends real money on real model APIs. Two
requests can carry the same key and arrive at once, so a pre-check alone loses
the race; the unique index on `idempotency_key` is what actually decides, and
the loser reads back the winner's row. Both checks are needed: the pre-check
keeps the common case cheap, the constraint makes it correct.

Raises domain errors (below), never HTTPException — the routers map them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.dataset import Dataset, DatasetRun
from app.models.enums import JobStatus, TaskStatus
from app.models.job import Job
from app.models.task import Task
from app.services.agents import AgentConfigError, resolve_agent
from app.services.event_bus import create_bus


class JobServiceError(Exception):
    """Base for the failures a caller is expected to handle."""


class NotFound(JobServiceError):
    """The task or dataset doesn't exist."""


class NotRunnable(JobServiceError):
    """It exists but can't be run — a task that never cleared Stage 1, or a
    dataset with no ready tasks in it."""


class Conflict(JobServiceError):
    """A constraint rejected the insert and it wasn't the idempotency key."""


@dataclass
class CreatedJob:
    """A job, plus whether we actually created it.

    `existing=True` means an idempotent retry returned the original run. Callers
    that log, meter, or bill care about the difference; the HTTP response doesn't.
    """

    job: Job
    existing: bool


@dataclass
class CreatedRun:
    run: DatasetRun
    tasks: Sequence[Task]
    existing: bool


async def create_job(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    agent: str,
    model: Optional[str],
    n_trials: int,
    concurrency: int,
    idempotency_key: Optional[str] = None,
) -> CreatedJob:
    """Queue one task for N trials.

    Returns as soon as the row is committed. Execution is a worker's job: the
    QUEUED row *is* the queue, so it doesn't matter whether the worker is
    embedded in this process or on another machine.
    """
    if idempotency_key:
        prior = await _job_by_key(db, idempotency_key)
        if prior is not None:
            return CreatedJob(prior, existing=True)

    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise NotFound("Task not found.")
    if task.status != TaskStatus.READY:
        raise NotRunnable("Task isn't ready for examination. Complete Stage 1 first.")

    resolved_agent, resolved_model = resolve_agent(agent, model, settings)

    job = Job(
        id=uuid.uuid4(),
        task_id=task_id,
        agent=resolved_agent,
        model=resolved_model,
        n_trials=n_trials,
        concurrency=concurrency,
        status=JobStatus.QUEUED.value,
        idempotency_key=idempotency_key,
    )
    db.add(job)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request with the same key won the race. Return its job;
        # do NOT enqueue a second (paid) run.
        await db.rollback()
        prior = await _job_by_key(db, idempotency_key) if idempotency_key else None
        if prior is None:
            raise Conflict("Conflict creating job.") from None
        return CreatedJob(prior, existing=True)
    await db.refresh(job)

    # Register the bus before returning so an SSE client that connects
    # immediately doesn't miss the worker's first events. In-process only — a
    # cross-machine worker's events never reach this bus and that client falls
    # back to the DB snapshot.
    create_bus(str(job.id))
    return CreatedJob(job, existing=False)


async def create_dataset_run(
    db: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    agent: str,
    model: Optional[str],
    n_trials: int,
    concurrency: int,
    idempotency_key: Optional[str] = None,
) -> CreatedRun:
    """Queue every ready task in a dataset against one agent+model.

    The run row is orchestration, not execution: it goes straight to RUNNING and
    is never claimed by a worker. Its child jobs are queued individually so one
    cohort's tasks spread across every worker; the last child to finish finalizes
    the run (job_queue.finalize_run_if_done).
    """
    if idempotency_key:
        prior = await _run_by_key(db, idempotency_key)
        if prior is not None:
            return CreatedRun(prior, tasks=[], existing=True)

    ds = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
    if ds is None:
        raise NotFound("Dataset not found.")

    resolved_agent, resolved_model = resolve_agent(agent, model, settings)

    # Manifest order matters (it's the leaderboard's column order), so index the
    # rows and walk the manifest rather than using the query's ordering.
    ids = [uuid.UUID(t) for t in (ds.task_ids or [])]
    rows = (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all() if ids else []
    by_id = {t.id: t for t in rows}
    ready = [by_id[i] for i in ids if i in by_id and by_id[i].status == TaskStatus.READY]
    if not ready:
        raise NotRunnable("No tasks in this dataset are ready for examination.")

    run = DatasetRun(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        agent=resolved_agent,
        model=resolved_model,
        n_trials=n_trials,
        concurrency=concurrency,
        status=JobStatus.RUNNING.value,
        idempotency_key=idempotency_key,
    )
    db.add(run)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prior = await _run_by_key(db, idempotency_key) if idempotency_key else None
        if prior is None:
            raise Conflict("Conflict creating dataset run.") from None
        return CreatedRun(prior, tasks=[], existing=True)
    await db.refresh(run)

    for t in ready:
        db.add(
            Job(
                id=uuid.uuid4(),
                task_id=t.id,
                dataset_run_id=run.id,
                agent=resolved_agent,
                model=resolved_model,
                n_trials=n_trials,
                concurrency=concurrency,
                status=JobStatus.QUEUED.value,
            )
        )
    await db.commit()

    # No single worker owns a run, so the run_started event is emitted here.
    # Children push task_progress; the finalizer pushes run_done.
    bus = create_bus(str(run.id))
    bus.emit(
        {
            "type": "run_started",
            "agent": resolved_agent,
            "model": resolved_model,
            "n_trials": n_trials,
            "concurrency": concurrency,
            "task_count": len(ready),
        }
    )
    return CreatedRun(run, tasks=ready, existing=False)


async def list_jobs(
    db: AsyncSession,
    *,
    task_id: Optional[uuid.UUID] = None,
    limit: int,
    offset: int,
) -> tuple[Sequence[Job], dict, int]:
    """A page of jobs (newest first) with their trials eager-loaded, the task
    names they need for display, and the total for pagination.

    `selectinload` on trials is load-bearing: the usage rollup walks every
    trial's trajectory, and without it a 100-job page issues 100 extra queries.
    """
    from sqlalchemy import func
    from sqlalchemy.orm import selectinload

    count_stmt = select(func.count()).select_from(Job)
    stmt = (
        select(Job)
        .options(selectinload(Job.trials))
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if task_id:
        stmt = stmt.where(Job.task_id == task_id)
        count_stmt = count_stmt.where(Job.task_id == task_id)

    total = (await db.execute(count_stmt)).scalar_one()
    jobs = (await db.execute(stmt)).scalars().unique().all()

    task_ids = {j.task_id for j in jobs}
    names: dict = {}
    if task_ids:
        rows = (await db.execute(select(Task.id, Task.name).where(Task.id.in_(task_ids)))).all()
        names = {r[0]: r[1] for r in rows}
    return jobs, names, total


async def _job_by_key(db: AsyncSession, key: str) -> Optional[Job]:
    return (await db.execute(select(Job).where(Job.idempotency_key == key))).scalar_one_or_none()


async def _run_by_key(db: AsyncSession, key: str) -> Optional[DatasetRun]:
    return (
        await db.execute(select(DatasetRun).where(DatasetRun.idempotency_key == key))
    ).scalar_one_or_none()


__all__ = [
    "AgentConfigError",
    "Conflict",
    "CreatedJob",
    "CreatedRun",
    "JobServiceError",
    "NotFound",
    "NotRunnable",
    "create_dataset_run",
    "create_job",
    "list_jobs",
]
