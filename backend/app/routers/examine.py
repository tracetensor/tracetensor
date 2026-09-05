"""
Examination Room — Stage 2 endpoints.

  POST /examine/{task_id}       start a job: run the agent through N trials
  GET  /examine/job/{job_id}    full job with trials + trajectories
  GET  /examine/jobs            list jobs (optionally by task)
  GET  /examine/task/{id}/jobs  jobs for a specific task

Trials run in a background thread so the POST returns immediately with a job id;
the frontend polls the job until status == completed.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import rate_limit_jobs
from app.models.job import Job, Trial
from app.routers._shared import should_tail, tail_bus, validate_trial_params
from app.routers._shared import sse as _sse
from app.routers._views import _job_summary, _job_view
from app.schemas.common import ERROR_RESPONSES, SSE_EVENT_REF, Page
from app.schemas.events import StreamEventEnvelope
from app.schemas.examine import (
    ExamineRequest,
    JobSummary,
    JobView,
    ProvidersView,
)
from app.services import job_service
from app.services.agents import AgentConfigError

router = APIRouter(prefix="/examine", tags=["Examination Room"])


@router.get(
    "/stream-events",
    response_model=StreamEventEnvelope,
    include_in_schema=True,
    summary="Live-progress event shapes (schema only)",
)
async def stream_event_shapes() -> None:
    """Documentation endpoint: publishes the SSE event union into OpenAPI.

    The `/stream` endpoints send `text/event-stream`, which FastAPI can't derive
    a response model from — so without a route that references it, the event
    models never reach `components.schemas` and a generated client has no types
    for the thing it spends most of its time parsing. Calling this returns 501;
    it exists for the schema.
    """
    raise HTTPException(status_code=501, detail="Schema-only endpoint. Use /stream.")


@router.get("/providers", response_model=ProvidersView, responses=ERROR_RESPONSES)
async def list_providers() -> ProvidersView:
    """Providers + their model catalog for the UI dropdowns.

    Each provider is available only if its API key is set (env or backend/.env).
    Backends list local + cloud sandboxes with honest `ready` flags from preflight.
    """
    from app.core.config import settings
    from app.services import agents as agent_svc
    from app.services import llm
    from app.services.backend_catalog import list_backend_entries

    return ProvidersView(
        agents=llm.catalog(settings.available_providers()),
        installed_agents=agent_svc.agent_status_catalog(),
        backends=list_backend_entries(),
    )


@router.post(
    "/{task_id}",
    response_model=JobView,
    dependencies=[Depends(rate_limit_jobs)],
    responses=ERROR_RESPONSES,
)
async def start_examination(
    task_id: uuid.UUID,
    req: ExamineRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JobView:
    """Begin Stage 2: run the task's agent through N trials and score each.

    Idempotent: a retried POST carrying the same `Idempotency-Key` header returns
    the already-created job instead of starting a second (double-charging) run.
    """
    # Everything below the parameter checks is app logic, not HTTP: see
    # app.services.job_service.create_job. This handler validates the request
    # shape, calls the service, and maps its errors onto status codes.
    concurrency = validate_trial_params(req.n_trials, req.concurrency, ceiling=req.n_trials)
    from app.services.backend_catalog import backend_not_ready_message, normalize_backend

    try:
        normalize_backend(req.backend)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    block = backend_not_ready_message(req.backend)
    if block:
        raise HTTPException(status_code=400, detail=block)
    try:
        created = await job_service.create_job(
            db,
            task_id=task_id,
            agent=req.agent,
            model=req.model,
            n_trials=req.n_trials,
            concurrency=concurrency,
            backend=req.backend,
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
    except job_service.NotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except job_service.NotRunnable as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except job_service.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AgentConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return await _job_view(db, created.job)


@router.get("/jobs", response_model=Page[JobSummary], responses=ERROR_RESPONSES)
async def list_jobs(
    task_id: Optional[uuid.UUID] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[JobSummary]:
    """Jobs, newest first, optionally filtered to one task."""
    jobs, names, total = await job_service.list_jobs(
        db, task_id=task_id, limit=limit, offset=offset
    )
    return Page.of([_job_summary(j, names.get(j.task_id)) for j in jobs], total, limit, offset)


@router.get("/job/{job_id}", response_model=JobView, responses=ERROR_RESPONSES)
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> JobView:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return await _job_view(db, job)


@router.get(
    "/job/{job_id}/stream",
    responses={
        200: {
            "description": (
                "A Server-Sent Events stream. Each `data:` line is one JSON "
                "object from the `StreamEvent` union — see StreamEventEnvelope "
                "for the per-`type` shapes."
            ),
            "content": {"text/event-stream": {"schema": {"$ref": SSE_EVENT_REF}}},
        },
        **ERROR_RESPONSES,
    },
)
async def stream_job(
    job_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Any:
    """Server-Sent Events: live task-progress for a running examination.

    Replays the bus history, then tails live until the job is done. If the job
    already finished (no live bus), reconstructs a terminal snapshot from the DB
    so the client's checklist still fills in.

    Times out after WORKER_LEASE_TIMEOUT * 2 seconds so a crashed worker doesn't
    leave streams open forever.
    """

    from app.services.event_bus import get_bus, has_events

    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    bus = get_bus(str(job_id))
    live = should_tail(job.status, await has_events(str(job_id)))

    async def gen() -> Any:
        # Already-finished job with no live bus → emit a snapshot and close.
        if not live:
            trials = (
                (
                    await db.execute(
                        select(Trial).where(Trial.job_id == job_id).order_by(Trial.trial_num)
                    )
                )
                .scalars()
                .all()
            )
            yield _sse(
                {
                    "type": "job_started",
                    "agent": job.agent,
                    "model": job.model,
                    "n_trials": job.n_trials,
                }
            )
            for t in trials:
                yield _sse({"type": "trial_started", "trial_num": t.trial_num})
                yield _sse(
                    {
                        "type": "trial_done",
                        "trial_num": t.trial_num,
                        "status": t.status,
                        "reward": t.reward,
                        "passed": t.passed,
                        "duration_s": t.duration_s,
                        "error": t.error,
                    }
                )
            yield _sse(
                {
                    "type": "job_done",
                    "status": job.status,
                    "trials_passed": job.trials_passed,
                    "n_trials": job.n_trials,
                    "pass_rate": job.pass_rate,
                }
            )
            return

        # Live job: replay history then tail until done.
        async for chunk in tail_bus(bus, request):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/task/{task_id}/jobs", response_model=Page[JobSummary], responses=ERROR_RESPONSES)
async def jobs_for_task(
    task_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[JobSummary]:
    """Jobs for one task — the same list, pre-filtered."""
    return await list_jobs(task_id=task_id, limit=limit, offset=offset, db=db)
