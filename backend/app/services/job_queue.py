"""
Durable job queue — the jobs/dataset_runs tables ARE the queue.

A worker claims the oldest queued item by atomically flipping it to 'running'
and stamping its lease (worker_id + claimed_at + heartbeat_at). On Postgres the
claim uses `SELECT ... FOR UPDATE SKIP LOCKED`, so any number of workers on any
number of machines pull work without ever colliding or blocking each other.
SQLite (dev/tests) has no SKIP LOCKED, but its database-level write lock
serializes writers, so a guarded conditional UPDATE is equally safe there.

The claimable unit is a Job. A standalone job is one task; a dataset run's
per-task jobs are ALSO claimed independently, so one cohort's tasks spread across
every worker (the dataset run itself is an orchestration record, not an execution
unit — it's finalized when its last child job finishes; see finalize_run_if_done).

Liveness: a running job whose heartbeat is older than the lease timeout is
presumed dead (crashed/restarted worker) and reclaimed — marked failed rather
than silently retried, since re-running could double-charge a real LLM.

Everything except the Postgres SKIP-LOCKED claim uses ORM constructs so the
UUID/timestamp column types bind correctly on both Postgres and SQLite (raw SQL
with a bound uuid.UUID param does not round-trip on SQLite).
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.dataset import DatasetRun
from app.models.enums import JobStatus
from app.models.job import Job

# String keys used by callers (executor passes these back for heartbeat).
JOBS = "jobs"
RUNS = "dataset_runs"
_MODEL: dict[str, Any] = {JOBS: Job, RUNS: DatasetRun}


def worker_identity() -> str:
    """Stable id for this worker process (config override, else host:pid)."""
    return settings.WORKER_ID or f"{socket.gethostname()}:{os.getpid()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "postgresql"


async def _claim(
    session: AsyncSession, model: Any, worker_id: str, pg_where: str, orm_filters: list
) -> Optional[uuid.UUID]:
    now = _now()
    if _is_postgres(session):
        # Atomic claim: the inner SELECT locks exactly one queued row and skips
        # any a concurrent worker already holds, so no two workers get the same
        # one. No Python UUID is bound here, so raw SQL is safe.
        table = model.__tablename__
        sql = text(
            f"""
            UPDATE {table} SET status='{JobStatus.RUNNING}', worker_id=:wid,
                                 claimed_at=:now, heartbeat_at=:now
            WHERE id = (
                SELECT id FROM {table}
                WHERE status='{JobStatus.QUEUED}' {pg_where}
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """
        )
        row = (await session.execute(sql, {"wid": worker_id, "now": now})).first()
        await session.commit()
        return row[0] if row else None

    # SQLite: no SKIP LOCKED. Writers are serialized by the DB lock, so pick the
    # oldest queued row then update it guarded by `status='queued'` (a racing
    # worker that already took it makes rowcount 0 → we simply got nothing).
    item_id = (
        await session.execute(
            select(model.id)
            .where(model.status == JobStatus.QUEUED, *orm_filters)
            .order_by(model.created_at)
            .limit(1)
        )
    ).scalar()
    if item_id is None:
        return None
    res = await session.execute(
        update(model)
        .where(model.id == item_id, model.status == JobStatus.QUEUED)
        .values(
            status=JobStatus.RUNNING.value, worker_id=worker_id, claimed_at=now, heartbeat_at=now
        )
    )
    await session.commit()
    return item_id if res.rowcount == 1 else None


async def claim_job(session: AsyncSession, worker_id: str) -> Optional[uuid.UUID]:
    """Claim the oldest queued job — standalone or a dataset-run child alike, so a
    cohort's tasks distribute across all workers."""
    return await _claim(session, Job, worker_id, "", [])


async def heartbeat(session: AsyncSession, table: str, item_id: uuid.UUID) -> None:
    """Refresh a running item's lease so reclaim doesn't presume it dead."""
    model = _MODEL[table]
    await session.execute(update(model).where(model.id == item_id).values(heartbeat_at=_now()))
    await session.commit()


async def finalize_run_if_done(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """If every child job of a run is terminal, atomically finalize the run.

    Called after a child job completes (fan-in). The guarded UPDATE means exactly
    one worker wins the race to finalize — it returns True and should emit the
    terminal event; everyone else returns False. overall_pass_rate etc. are
    derived from the child jobs at view time, so there's nothing else to roll up.
    """
    total = (
        await session.execute(
            select(func.count()).select_from(Job).where(Job.dataset_run_id == run_id)
        )
    ).scalar_one()
    if not total:
        return False
    done = (
        await session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.dataset_run_id == run_id, Job.status.in_(JobStatus.terminal()))
        )
    ).scalar_one()
    if done < total:
        return False
    res = await session.execute(
        update(DatasetRun)
        .where(DatasetRun.id == run_id, DatasetRun.status.notin_(JobStatus.terminal()))
        .values(status=JobStatus.COMPLETED.value, finished_at=_now())
    )
    await session.commit()
    return (res.rowcount or 0) == 1


async def reclaim_stale(session_factory: async_sessionmaker, lease_timeout: int) -> dict:
    """Fail jobs whose worker went silent (crash/restart), then finalize any
    dataset run whose children are now all terminal. Returns counts.

    Accepts a session FACTORY (not a session) so each finalization opens its own
    transaction — a failure on one run can't corrupt the session state for the
    next one. The bulk job-update and the run-id query also use their own sessions.
    """
    cutoff = _now() - timedelta(seconds=lease_timeout)
    now = _now()
    msg = "worker lost (lease expired) — re-run to retry"
    stale = or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff)

    async with session_factory() as session:
        jobs_res = await session.execute(
            update(Job)
            .where(Job.status == JobStatus.RUNNING, stale)
            .values(status=JobStatus.FAILED.value, error=msg, finished_at=now)
        )
        await session.commit()

        running_runs = (
            (
                await session.execute(
                    select(DatasetRun.id).where(DatasetRun.status == JobStatus.RUNNING)
                )
            )
            .scalars()
            .all()
        )

    finalized = 0
    for run_id in running_runs:
        async with session_factory() as fin_session:
            try:
                if await finalize_run_if_done(fin_session, run_id):
                    finalized += 1
            except Exception:
                await fin_session.rollback()

    return {"jobs": jobs_res.rowcount or 0, "dataset_runs": finalized}
