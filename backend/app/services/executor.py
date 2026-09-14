"""
Job execution — runs a CLAIMED job or dataset run to completion.

The worker (app.worker) hands these functions only an id; everything else is
reconstructed from the database, so the same code runs whether the job was
enqueued on this machine or another. Each function:
  - assumes the item is already 'running' (the queue claim set that + the lease),
  - keeps the lease fresh with a background heartbeat while it works,
  - streams progress to the in-process event bus (live SSE on single-node; a
    cross-machine worker's events just don't reach the web tier, which then
    falls back to the DB snapshot — see routers' stream endpoints),
  - writes the terminal status.

The trial-running core (run_trial) is untouched; this only orchestrates it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import JobStatus
from app.models.job import Job, Trial
from app.models.task import Task
from app.services import diagnose, job_queue
from app.services.event_bus import JobBus, create_bus, get_bus
from app.services.trial_runner import prebuild, run_trial

log = get_logger("tracetensor.executor")


def _bus_for(item_id: uuid.UUID) -> JobBus:
    """The item's event bus (reuse the one the POST created, else make one)."""
    return get_bus(str(item_id)) or create_bus(str(item_id))


async def _heartbeat_loop(SessionLocal: async_sessionmaker, table: str, item_id: uuid.UUID) -> None:
    """Refresh the lease every WORKER_HEARTBEAT_INTERVAL until cancelled."""
    try:
        while True:
            await asyncio.sleep(settings.WORKER_HEARTBEAT_INTERVAL)
            async with SessionLocal() as hb:
                await job_queue.heartbeat(hb, table, item_id)
    except asyncio.CancelledError:
        pass


async def execute_job(SessionLocal: async_sessionmaker, job_id: uuid.UUID) -> None:
    """Run one job (already claimed → 'running') through N trials.

    Works for both a standalone job and a dataset-run child. For a child it also
    fans results back to the run's event bus and, when it's the last child to
    finish, finalizes the run (see job_queue.finalize_run_if_done)."""
    bus = _bus_for(job_id)

    def emit(evt: dict) -> None:
        bus.emit(evt)

    run_id: Optional[uuid.UUID] = None
    task_name: Optional[str] = None
    passed = 0
    n_trials = 0
    hb = asyncio.create_task(_heartbeat_loop(SessionLocal, job_queue.JOBS, job_id))
    try:
        # Short-lived session: read job+task metadata, then release the connection.
        async with SessionLocal() as db:
            job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
            task = (await db.execute(select(Task).where(Task.id == job.task_id))).scalar_one()
            run_id = job.dataset_run_id
            task_name = task.name
            agent, model, n_trials = job.agent, job.model, job.n_trials
            backend = job.backend or "docker"
            task_id = job.task_id
            task_dir_str = task.task_dir
            concurrency = min(job.concurrency or 4, n_trials or 1)
            timeout_agent = task.timeout_agent_sec
            timeout_verifier = task.timeout_verifier_sec

        ipath = Path(task_dir_str) / "instruction.md"
        instruction = ipath.read_text(errors="replace") if ipath.exists() else ""

        emit(
            {
                "type": "job_started",
                "agent": agent,
                "model": model,
                "backend": backend,
                "n_trials": n_trials,
                "concurrency": concurrency,
            }
        )
        if concurrency > 1:
            await asyncio.to_thread(prebuild, Path(task_dir_str), backend)

        passed = 0
        scored = 0
        completed = 0
        sem = asyncio.Semaphore(concurrency)
        db_lock = asyncio.Lock()

        async def _one(i: int) -> None:
            nonlocal passed, scored, completed

            def _tag(e: dict, n: int = i) -> None:
                emit({**e, "trial_num": n})

            async with sem:
                emit({"type": "trial_started", "trial_num": i})
                outcome = await asyncio.to_thread(
                    run_trial,
                    task_dir=Path(task_dir_str),
                    instruction=instruction,
                    agent_name=agent,
                    model=model,
                    backend=backend,
                    agent_timeout=timeout_agent,
                    verifier_timeout=timeout_verifier,
                    on_event=_tag,
                )
            # Diagnose, rules tier: pure-function classification of why the
            # trial failed (docs/DIAGNOSE.md). Free and offline, so it runs
            # inline before the write — one insert carries the analysis. The
            # LLM tier runs after trial_done, never on this path.
            if settings.DIAGNOSE_ENABLED:
                trial_dict = {
                    "status": outcome.status,
                    "passed": outcome.passed,
                    "reward": outcome.reward,
                    "error": outcome.error,
                    "trajectory": outcome.trajectory or {},
                }
                analysis = diagnose.diagnose_trial(
                    trial_dict,
                    max_steps=settings.LLM_AGENT_MAX_STEPS,
                    cost_limit_usd=settings.AGENT_COST_LIMIT_USD,
                )
                outcome.trajectory = {**(outcome.trajectory or {}), "failure_analysis": analysis}

            trial_id = uuid.uuid4()
            # Short-lived session per trial write — released immediately.
            async with db_lock:
                async with SessionLocal() as db:
                    db.add(
                        Trial(
                            id=trial_id,
                            job_id=job_id,
                            task_id=task_id,
                            trial_num=i,
                            status=outcome.status,
                            reward=outcome.reward,
                            passed=outcome.passed,
                            trajectory=outcome.trajectory,
                            verifier_log=outcome.verifier_log,
                            reward_payload=outcome.reward_payload,
                            duration_s=outcome.duration_s,
                            error=outcome.error,
                        )
                    )
                    if outcome.passed is not None:
                        scored += 1
                    if outcome.passed:
                        passed += 1
                    completed += 1
                    await db.execute(select(Job).where(Job.id == job_id).with_for_update())
                    job_row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
                    job_row.trials_completed = completed
                    job_row.trials_passed = passed
                    await db.commit()
            emit(
                {
                    "type": "trial_done",
                    "trial_num": i,
                    "status": outcome.status,
                    "reward": outcome.reward,
                    "passed": outcome.passed,
                    "duration_s": outcome.duration_s,
                    "error": outcome.error,
                    "warnings": outcome.warnings,
                }
            )

            # Diagnose, LLM tier (opt-in — it spends money). Runs strictly
            # after trial_done so the trial result is never delayed, outside
            # the trial semaphore so it never holds a sandbox slot. Failures
            # here only log; a broken diagnosis must not fail a recorded trial.
            if settings.DIAGNOSE_ENABLED and settings.DIAGNOSE_LLM_ENABLED:
                try:
                    from app.services import diagnose_llm

                    trial_dict = {
                        "status": outcome.status,
                        "passed": outcome.passed,
                        "reward": outcome.reward,
                        "error": outcome.error,
                        "trajectory": outcome.trajectory or {},
                    }
                    analysis = await asyncio.to_thread(
                        diagnose_llm.diagnose_trial_full,
                        trial_dict,
                        instruction,
                        model_spec=settings.DIAGNOSE_MODEL,
                        max_steps=settings.LLM_AGENT_MAX_STEPS,
                        cost_limit_usd=settings.AGENT_COST_LIMIT_USD,
                    )
                    async with db_lock:
                        async with SessionLocal() as db:
                            row = (
                                await db.execute(select(Trial).where(Trial.id == trial_id))
                            ).scalar_one()
                            row.trajectory = {
                                **(row.trajectory or {}),
                                "failure_analysis": analysis,
                            }
                            await db.commit()
                except Exception as e:  # noqa: BLE001 — diagnosis is best-effort
                    log.warning("diagnose_llm_failed", extra={"trial": i, "error": str(e)[:200]})
                    # The rules block was already persisted inline — report that.
                    analysis = (outcome.trajectory or {}).get("failure_analysis")
            else:
                analysis = (outcome.trajectory or {}).get("failure_analysis")
            if analysis:
                emit(
                    {
                        "type": "diagnose",
                        "trial_num": i,
                        "primary": analysis.get("primary"),
                        "occurrence_count": len(analysis.get("occurrences") or []),
                        "engine": analysis.get("engine", "rules"),
                    }
                )

        await asyncio.gather(*[_one(i) for i in range(n_trials)])

        async with SessionLocal() as db:
            job_row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
            job_row.status = JobStatus.COMPLETED.value
            job_row.pass_rate = (passed / scored) if scored else None
            job_row.finished_at = datetime.now(timezone.utc)
            await db.commit()
            final_pass_rate = job_row.pass_rate
        emit(
            {
                "type": "job_done",
                "status": JobStatus.COMPLETED.value,
                "trials_passed": passed,
                "n_trials": n_trials,
                "pass_rate": final_pass_rate,
            }
        )
        # Dataset-run child: report to the run and, if last, finalize it.
        if run_id is not None:
            await _child_fanin(
                SessionLocal, run_id, job_id, task_name, passed, n_trials, JobStatus.COMPLETED
            )
    except Exception as e:
        await _fail(SessionLocal, Job, job_id, str(e))
        emit({"type": "job_done", "status": JobStatus.FAILED.value, "error": str(e)[:500]})
        if run_id is not None:
            await _child_fanin(
                SessionLocal, run_id, job_id, task_name, passed, n_trials, JobStatus.FAILED
            )
    finally:
        hb.cancel()


async def _child_fanin(
    SessionLocal: async_sessionmaker,
    run_id: uuid.UUID,
    job_id: uuid.UUID,
    task_name: Optional[str],
    passed: int,
    n_trials: int,
    status: str,
) -> None:
    """A dataset-run child finished — push a task_progress event to the run's bus
    (live SSE on single-node; a cross-machine worker just won't reach it) and, if
    this was the last child, finalize the run and emit its terminal event."""
    run_bus = get_bus(str(run_id))
    if run_bus is not None:
        run_bus.emit(
            {
                "type": "task_progress",
                "job_id": str(job_id),
                "task_name": task_name,
                "trials_completed": n_trials,
                "trials_passed": passed,
                "n_trials": n_trials,
                "status": status,
            }
        )
    async with SessionLocal() as db:
        finalized = await job_queue.finalize_run_if_done(db, run_id)
    if finalized and run_bus is not None:
        run_bus.emit({"type": "run_done", "status": JobStatus.COMPLETED.value})


async def _fail(
    SessionLocal: async_sessionmaker,
    model_cls: Any,
    item_id: uuid.UUID,
    err: str,
) -> None:
    """Best-effort terminal 'failed' write on an unexpected error."""
    try:
        async with SessionLocal() as db2:
            item = (
                await db2.execute(select(model_cls).where(model_cls.id == item_id))
            ).scalar_one_or_none()
            if item is not None:
                item.status = JobStatus.FAILED.value
                item.error = err[:2000]
                item.finished_at = datetime.now(timezone.utc)
                await db2.commit()
    except Exception:
        # If even the terminal write fails, the row stays "running" forever and the
        # worker's lease reclaim is the only thing that will ever move it. Log it —
        # a silently stuck job is the hardest failure mode in this system to debug.
        log.exception(
            "terminal_write_failed",
            extra={"item_id": str(item_id), "model": model_cls.__name__},
        )


def make_worker_engine() -> AsyncEngine:
    """A dedicated NullPool engine for a worker's event loop.

    Workers run in their own thread/loop (embedded) or process (standalone), and
    asyncpg connections are bound to the loop that made them — so a worker must
    NOT share the app's engine. NullPool opens/closes per use, which suits a
    long-lived poll loop that's mostly idle.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(settings.DATABASE_URL, poolclass=NullPool, future=True)
