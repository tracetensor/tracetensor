"""
Datasets — Patient Cohorts (versioned bundles of tasks).

  POST /datasets/upload            register a bundle (dataset.toml + task folders)
  GET  /datasets                   list datasets
  GET  /datasets/{id}              dataset detail (its tasks)
  POST /datasets/{id}/examine      run agent+model across all tasks (one run)
  GET  /datasets/runs/{run_id}     run status + scoreboard
  GET  /datasets/{id}/runs         runs for a dataset

A dataset run creates one queued Job per task; those jobs are claimed
independently by any worker, so a cohort's tasks distribute across every worker
and machine. The run itself is an orchestration record, finalized when its last
child job finishes (see app.services.job_queue.finalize_run_if_done).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import rate_limit_jobs
from app.models.dataset import Dataset, DatasetRun
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.task import Task
from app.routers._shared import (
    MAX_CONCURRENCY,
    MAX_DATASET_ZIP_BYTES,
    read_upload,
    should_tail,
    tail_bus,
    validate_trial_params,
)
from app.routers._shared import sse as _sse
from app.schemas.common import (
    ERROR_RESPONSES,
    SSE_EVENT_REF,
    Page,
    as_job_status,
    as_optional_job_status,
    as_task_status,
)
from app.schemas.dataset import (
    DatasetRunRequest,
    DatasetRunSummary,
    DatasetRunView,
    DatasetSummary,
    DatasetTaskInfo,
    DatasetView,
    LeaderboardCell,
    LeaderboardRow,
    LeaderboardTask,
    LeaderboardView,
    TaskScore,
)
from app.services import dataset_service, job_service
from app.services.agents import AgentConfigError
from app.services.dataset_parser import (
    DatasetParseError,
    compute_content_hash,
)
from app.services.task_parser import TaskParseError, parse_task_toml
from app.services.task_service import (
    EXAMPLES_DIR,
    gather_files,
    persist_task,
)
from app.storage.task_store import store_task_from_files

router = APIRouter(prefix="/datasets", tags=["Datasets"])


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------
async def _dataset_view(db: AsyncSession, ds: Dataset) -> DatasetView:
    ids = [uuid.UUID(t) for t in (ds.task_ids or [])]
    tasks = []
    if ids:
        rows = (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
        by_id = {t.id: t for t in rows}
        for tid in ids:
            t = by_id.get(tid)
            if t is not None:
                tasks.append(
                    DatasetTaskInfo(task_id=t.id, name=t.name, status=as_task_status(t.status))
                )
    return DatasetView(
        id=ds.id,
        name=ds.name,
        version=ds.version,
        description=ds.description,
        content_hash=ds.content_hash,
        task_count=len(ds.task_ids or []),
        tasks=tasks,
        created_at=ds.created_at,
    )


async def _run_view(db: AsyncSession, run: DatasetRun) -> DatasetRunView:
    ds = (
        await db.execute(select(Dataset).where(Dataset.id == run.dataset_id))
    ).scalar_one_or_none()
    jobs = (await db.execute(select(Job).where(Job.dataset_run_id == run.id))).scalars().all()

    # Names for the scoreboard.
    task_ids = [j.task_id for j in jobs]
    names = {}
    if task_ids:
        rows = (await db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all()
        names = {t.id: t.name for t in rows}

    scores = [
        TaskScore(
            task_id=j.task_id,
            task_name=names.get(j.task_id, "?"),
            job_id=j.id,
            status=as_job_status(j.status),
            n_trials=j.n_trials,
            trials_completed=j.trials_completed,
            trials_passed=j.trials_passed,
            pass_rate=j.pass_rate,
        )
        for j in jobs
    ]
    rates = [s.pass_rate for s in scores if s.pass_rate is not None]
    overall = sum(rates) / len(rates) if rates else None
    tasks_completed = sum(1 for s in scores if s.status == JobStatus.COMPLETED)

    return DatasetRunView(
        id=run.id,
        dataset_id=run.dataset_id,
        dataset_name=ds.name if ds else "?",
        dataset_version=ds.version if ds else "?",
        agent=run.agent,
        model=run.model,
        n_trials=run.n_trials,
        concurrency=run.concurrency,
        status=as_job_status(run.status),
        error=run.error,
        created_at=run.created_at,
        finished_at=run.finished_at,
        task_count=len(scores),
        tasks_completed=tasks_completed,
        overall_pass_rate=overall,
        scores=scores,
    )


# ----------------------------------------------------------------------
# Upload / read
# ----------------------------------------------------------------------
@router.post("/upload", response_model=DatasetView, responses=ERROR_RESPONSES)
async def upload_dataset(
    file: UploadFile = File(..., description="ZIP bundle: dataset.toml + task folders"),
    db: AsyncSession = Depends(get_db),
) -> DatasetView:
    """Register a dataset bundle. Each task folder is registered like a normal
    task; the dataset records the manifest + a content hash for versioning.

    Idempotent: re-uploading identical bytes returns the existing dataset, so an
    interrupted upload is safe to retry.
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a .zip bundle.")

    zip_bytes = await read_upload(file, MAX_DATASET_ZIP_BYTES)
    try:
        ds, _created = await dataset_service.register_bundle(db, zip_bytes, settings.TASKS_ROOT)
    except DatasetParseError as e:
        raise HTTPException(status_code=400, detail=f"Bad bundle: {e}") from e
    except dataset_service.DatasetVersionConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return await _dataset_view(db, ds)


@router.get("", response_model=Page[DatasetSummary], responses=ERROR_RESPONSES)
async def list_datasets(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[DatasetSummary]:
    """Datasets, newest first. Paginated — this used to be a full table scan."""
    total = (await db.execute(select(func.count()).select_from(Dataset))).scalar_one()
    rows = (
        (
            await db.execute(
                select(Dataset).order_by(Dataset.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    items = [
        DatasetSummary(
            id=d.id,
            name=d.name,
            version=d.version,
            task_count=len(d.task_ids or []),
            created_at=d.created_at,
        )
        for d in rows
    ]
    return Page.of(items, total, limit, offset)


@router.post("/example", response_model=DatasetView, responses=ERROR_RESPONSES)
async def register_example_dataset(db: AsyncSession = Depends(get_db)) -> DatasetView:
    """One-click: register a sample dataset (example-suite: sort-csv + log-analyzer)."""
    folders = ["sort-csv", "log-analyzer"]
    task_files: dict = {}
    for fld in folders:
        src = EXAMPLES_DIR / fld
        if not (src / "task.toml").exists():
            raise HTTPException(status_code=404, detail=f"Example '{fld}' not found on the server.")
        task_files[fld] = gather_files(src)

    content_hash = compute_content_hash(task_files)
    existing = (
        await db.execute(
            select(Dataset).where(Dataset.name == "example-suite", Dataset.version == "1.0")
        )
    ).scalar_one_or_none()
    if existing:
        return await _dataset_view(db, existing)  # idempotent

    task_ids, task_names = [], []
    for fld in folders:
        files = task_files[fld]
        try:
            tname = parse_task_toml(files["task.toml"], Path(fld)).name
        except TaskParseError:
            tname = fld
        task_dir = store_task_from_files(settings.TASKS_ROOT, tname, files)
        task, _ = await persist_task(db, task_dir)
        task_ids.append(str(task.id))
        task_names.append(task.name)

    ds = Dataset(
        id=uuid.uuid4(),
        name="example-suite",
        version="1.0",
        content_hash=content_hash,
        task_ids=task_ids,
        task_names=task_names,
        description="Example cohort — sort-csv + log-analyzer.",
    )
    db.add(ds)
    await db.commit()
    await db.refresh(ds)
    return await _dataset_view(db, ds)


@router.get("/runs", response_model=Page[DatasetRunSummary], responses=ERROR_RESPONSES)
async def list_all_dataset_runs(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[DatasetRunSummary]:
    """All dataset runs, newest first — used for sidebar activity (in-flight runs)."""
    total = (await db.execute(select(func.count()).select_from(DatasetRun))).scalar_one()
    runs = (
        (
            await db.execute(
                select(DatasetRun)
                .order_by(DatasetRun.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    items = [
        DatasetRunSummary(
            id=r.id,
            dataset_id=r.dataset_id,
            agent=r.agent,
            model=r.model,
            n_trials=r.n_trials,
            status=as_job_status(r.status),
            overall_pass_rate=None,  # kept cheap; per-dataset endpoint computes it
            created_at=r.created_at,
        )
        for r in runs
    ]
    return Page.of(items, total, limit, offset)


@router.get("/{dataset_id}", response_model=DatasetView, responses=ERROR_RESPONSES)
async def get_dataset(dataset_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> DatasetView:
    ds = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return await _dataset_view(db, ds)


# ----------------------------------------------------------------------
# Run + scoreboard
# ----------------------------------------------------------------------
@router.post(
    "/{dataset_id}/examine",
    response_model=DatasetRunView,
    dependencies=[Depends(rate_limit_jobs)],
    responses=ERROR_RESPONSES,
)
async def start_dataset_run(
    dataset_id: uuid.UUID,
    req: DatasetRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DatasetRunView:
    """Run one agent+model across every ready task in the dataset, N trials each.

    Idempotent via the `Idempotency-Key` header — a retry returns the existing run.
    """
    # Thin by design — the orchestration lives in
    # app.services.job_service.create_dataset_run.
    concurrency = validate_trial_params(req.n_trials, req.concurrency, ceiling=MAX_CONCURRENCY)
    from app.services.backend_catalog import backend_not_ready_message, normalize_backend

    try:
        normalize_backend(req.backend)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    block = backend_not_ready_message(req.backend)
    if block:
        raise HTTPException(status_code=400, detail=block)
    try:
        created = await job_service.create_dataset_run(
            db,
            dataset_id=dataset_id,
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

    return await _run_view(db, created.run)


@router.get("/runs/{run_id}", response_model=DatasetRunView, responses=ERROR_RESPONSES)
async def get_dataset_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> DatasetRunView:
    run = (await db.execute(select(DatasetRun).where(DatasetRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Dataset run not found.")
    return await _run_view(db, run)


@router.get(
    "/runs/{run_id}/stream",
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
async def stream_dataset_run(
    run_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> Any:
    """Server-Sent Events: live progress for a running dataset (cohort) run.

    Mirrors /examine/job/{id}/stream: replays the bus history then tails live
    until done. If the run already finished (no live bus), reconstructs a
    terminal snapshot from the DB so a late/reconnecting client still fills in.
    Events are coarse (task_progress per trial) — the client re-fetches the
    scoreboard rather than us mirroring per-command detail here.
    """
    from app.services.event_bus import get_bus, has_events

    run = (await db.execute(select(DatasetRun).where(DatasetRun.id == run_id))).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Dataset run not found.")

    bus = get_bus(str(run_id))
    live = should_tail(run.status, await has_events(str(run_id)))

    async def gen() -> Any:
        if not live:
            view = await _run_view(db, run)
            yield _sse(
                {
                    "type": "run_started",
                    "agent": run.agent,
                    "model": run.model,
                    "n_trials": run.n_trials,
                    "concurrency": run.concurrency,
                    "task_count": view.task_count,
                }
            )
            for s in view.scores:
                yield _sse(
                    {
                        "type": "task_progress",
                        "task_id": str(s.task_id),
                        "task_name": s.task_name,
                        "job_id": str(s.job_id),
                        "trials_completed": s.trials_completed,
                        "trials_passed": s.trials_passed,
                        "n_trials": s.n_trials,
                        "status": s.status,
                    }
                )
            yield _sse({"type": "run_done", "status": run.status})
            return

        async for chunk in tail_bus(bus, request):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{dataset_id}/leaderboard", response_model=LeaderboardView, responses=ERROR_RESPONSES)
async def dataset_leaderboard(
    dataset_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> LeaderboardView:
    """Aggregate scoreboard: one row per (agent, model), one column per task.

    Each cell is the pass rate of that combo's *latest completed* run on that
    task (with the job id, so the UI can drill into the trajectory). Rows are
    sorted by overall pass rate, descending — a leaderboard for the dataset.
    """
    data = await dataset_service.build_leaderboard(db, dataset_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return LeaderboardView(
        dataset_id=dataset_id,
        dataset_name=data.dataset.name,
        version=data.dataset.version,
        tasks=[LeaderboardTask(task_id=c.task_id, name=c.name) for c in data.columns],
        rows=[
            LeaderboardRow(
                agent=r.agent,
                model=r.model,
                run_id=r.run_id,
                run_count=r.run_count,
                overall_pass_rate=r.overall_pass_rate,
                created_at=r.created_at,
                cells=[
                    LeaderboardCell(
                        task_id=c.task_id,
                        pass_rate=c.pass_rate,
                        job_id=c.job_id,
                        status=as_optional_job_status(c.status),
                    )
                    for c in r.cells
                ],
            )
            for r in data.rows
        ],
    )


@router.get("/{dataset_id}/runs", response_model=Page[DatasetRunSummary], responses=ERROR_RESPONSES)
async def list_dataset_runs(
    dataset_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[DatasetRunSummary]:
    """Runs for a dataset, newest first. Paginated — a long-lived dataset
    accumulates runs without bound, and this used to return all of them."""
    runs, rates_by_run, total = await dataset_service.list_runs_with_rates(
        db, dataset_id, limit=limit, offset=offset
    )
    out = []
    for run in runs:
        rates = rates_by_run.get(run.id, [])
        out.append(
            DatasetRunSummary(
                id=run.id,
                dataset_id=run.dataset_id,
                agent=run.agent,
                model=run.model,
                n_trials=run.n_trials,
                status=as_job_status(run.status),
                overall_pass_rate=(sum(rates) / len(rates) if rates else None),
                created_at=run.created_at,
            )
        )
    return Page.of(out, total, limit, offset)
