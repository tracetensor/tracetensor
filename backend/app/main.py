"""
TraceTensor API — entry point.

Registers tasks and datasets, runs agents through them in isolated Docker
sandboxes, and scores the results. Serves the frontend and the REST API.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError as _IntegrityError

from app.core.config import settings
from app.core.database import init_db
from app.core.logging import configure_logging, get_logger, request_id_var
from app.core.security import require_auth
from app.routers import datasets, examine, ingest, vault

configure_logging()
log = get_logger("tracetensor")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def _check_tasks_root() -> None:
    """Fail loudly at startup if TASKS_ROOT isn't writable rather than letting the
    first task upload crash with a cryptic OS error from deep inside task_store."""
    root = settings.TASKS_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_probe"
        probe.touch()
        probe.unlink()
    except OSError as e:
        log.error(
            "tasks_root_not_writable",
            extra={
                "path": str(root),
                "error": str(e),
                "fix": "Set TASKS_ROOT to a writable path, e.g. TASKS_ROOT=./_tasks",
            },
        )


def _warn_if_insecure() -> None:
    """H-1: Loud startup warning when running without auth on a non-loopback bind.

    API_TOKEN is intentionally off by default for localhost dev. But anyone who
    deploys this on a real host without setting it exposes the full API — including
    job creation that calls paid LLM APIs — to the internet. Warn loudly so the
    operator can't miss it.
    """
    if not settings.API_TOKEN:
        log.warning(
            "NO_AUTH_TOKEN_SET — all endpoints are unauthenticated. "
            "Set API_TOKEN=<secret> before exposing this instance beyond localhost. "
            "Without it, anyone who can reach the port can create jobs that spend your LLM budget."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    _warn_if_insecure()
    _check_tasks_root()
    await init_db()

    # Reclaim any queue leases orphaned by a previous crash/restart, so jobs that
    # were mid-run when the process died don't sit wedged in 'running' forever.
    # Respects live workers' heartbeats (a fresh lease is never touched).
    from app.core.database import AsyncSessionLocal
    from app.services import job_queue

    counts = await job_queue.reclaim_stale(AsyncSessionLocal, settings.WORKER_LEASE_TIMEOUT)
    if counts["jobs"] or counts["dataset_runs"]:
        log.warning("startup_reclaim", extra=counts)

    # Live progress goes through the database so it survives the process
    # boundary: a worker on another machine writes events the web tier can
    # actually serve. See app/services/event_bus.py.
    from app.services import event_bus

    bus_backend = await event_bus.use_database_backend(AsyncSessionLocal)
    pruned = await event_bus.prune_old_events(AsyncSessionLocal, settings.EVENT_RETENTION_SECONDS)
    if pruned:
        log.info("event_log_pruned", extra={"rows": pruned})

    # Single-node default: run a worker inside the API process. Set
    # WORKER_EMBEDDED=false to execute jobs on separate `python -m app.worker`
    # machines instead (the API then only enqueues).
    embedded = None
    if settings.WORKER_EMBEDDED:
        from app.worker import start_embedded_worker

        embedded = start_embedded_worker()
        log.info("embedded_worker_enabled", extra={"concurrency": settings.WORKER_CONCURRENCY})

    log.info("startup", extra={"app": settings.APP_NAME, "version": settings.APP_VERSION})
    yield

    # On shutdown, drain the embedded worker: stop claiming new work and give
    # in-flight jobs a bounded window to finish (rather than being abandoned and
    # later reclaimed as failed).
    if embedded is not None:
        drained = embedded.shutdown(settings.WORKER_DRAIN_TIMEOUT)
        log.info("embedded_worker_drained", extra={"clean": drained})

    # Flush whatever progress is still buffered, so the tail of a run that
    # finished during shutdown isn't lost.
    await bus_backend.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Self-hosted platform for evaluating coding agents.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    # M-3: do NOT set allow_credentials=True — the API uses Bearer tokens, not
    # cookies. Combining credentials=True with wildcard methods/headers is either
    # a spec violation (browser rejects) or dangerously permissive.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Any:
    """Attach a correlation id to every request (honoring an inbound
    X-Request-ID), bind it for all logs in this request, and emit one access
    log line with method/path/status/duration."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    token = request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
        ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["X-Request-ID"] = rid
        # H-4: log only the path, never the query string — the ?token= SSE
        # fallback would appear in plain text in every access log line.
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": ms,
            },
        )
        return response
    finally:
        request_id_var.reset(token)


# API is versioned under /v1 so a future breaking change can ship as /v2 without
# breaking existing clients. The unversioned paths are kept as DEPRECATED aliases
# (hidden from the docs) so anything already calling /examine, /ingest, /datasets
# keeps working through the transition. Ops endpoints (/api/health etc.) are
# intentionally unversioned and stable.
#
# When API_TOKEN is set, every API route requires it (require_auth is a no-op
# otherwise, so localhost/dev is unchanged). Health + the frontend shell stay
# open so probes and the initial page load work; the page authenticates its own
# API calls.
_API_ROUTERS = (ingest.router, examine.router, datasets.router, vault.router)
for _r in _API_ROUTERS:
    app.include_router(_r, prefix="/v1", dependencies=[Depends(require_auth)])
    app.include_router(
        _r, dependencies=[Depends(require_auth)], include_in_schema=False
    )  # deprecated unversioned alias


@app.exception_handler(_IntegrityError)
async def integrity_error_handler(request: Request, exc: _IntegrityError) -> Any:
    """A DB uniqueness/constraint violation is a client conflict, not a 500."""
    log.warning(
        "integrity_conflict",
        extra={
            "method": request.method,
            "path": request.url.path,
        },
    )
    return JSONResponse(
        status_code=409,
        content={"detail": "Conflict — that resource already exists."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> Any:
    """Catch-all so an unexpected error returns a clean JSON shape with an error
    id instead of leaking a stack trace to the client. (HTTPExceptions are still
    handled normally by FastAPI and keep their status/detail.)"""
    error_id = uuid.uuid4().hex[:12]
    log.error(
        "unhandled_exception",
        exc_info=exc,
        extra={
            "error_id": error_id,
            "method": request.method,
            "path": request.url.path,
        },
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error.", "error_id": error_id},
    )


@app.get("/api/health")
async def health() -> Any:
    """Liveness — is the process up? Cheap and dependency-free (for a fast probe
    that shouldn't flap when the DB blips). Use /api/ready for dependencies."""
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


@app.get("/api/ready")
async def ready() -> Any:
    """Readiness — can the app actually serve? Verifies the database is
    reachable; returns 503 (not ready) if not, so a load balancer stops routing
    to a node that can't talk to Postgres."""
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as e:
        # M-2: do NOT return str(e) — SQLAlchemy/asyncpg errors often contain
        # the full DATABASE_URL (user:password@host). Log internally, return
        # a safe opaque message to the caller.
        log.error("readiness_check_failed", exc_info=e)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "db": "down", "error": "database unreachable"},
        )
    return {"status": "ready", "db": "ok"}


@app.get("/api/metrics")
async def metrics() -> Any:
    """Prometheus metrics — queue depth and job/run counts by status, so you can
    watch throughput and backlog in production. Plain-text exposition format; no
    extra dependency. Left un-authed so a scraper can reach it (it exposes only
    aggregate counts, no payloads)."""
    from fastapi.responses import PlainTextResponse
    from sqlalchemy import func, select

    from app.core.database import AsyncSessionLocal
    from app.models.dataset import DatasetRun
    from app.models.enums import JobStatus
    from app.models.job import Job

    async with AsyncSessionLocal() as db:
        # dict(rows) doesn't narrow — SQLAlchemy hands back Row objects, which
        # mypy can't see as 2-tuples. Build the mapping explicitly so the types
        # are real and a shape change here is caught rather than inferred away.
        job_counts: dict[str, int] = {
            status: count
            for status, count in (
                await db.execute(select(Job.status, func.count()).group_by(Job.status))
            ).all()
        }
        run_counts: dict[str, int] = {
            status: count
            for status, count in (
                await db.execute(
                    select(DatasetRun.status, func.count()).group_by(DatasetRun.status)
                )
            ).all()
        }

    statuses = tuple(JobStatus.values())
    lines = [
        "# HELP tracetensor_jobs Jobs by status.",
        "# TYPE tracetensor_jobs gauge",
    ]
    lines += [f'tracetensor_jobs{{status="{s}"}} {job_counts.get(s, 0)}' for s in statuses]
    lines += [
        "# HELP tracetensor_dataset_runs Dataset runs by status.",
        "# TYPE tracetensor_dataset_runs gauge",
    ]
    lines += [f'tracetensor_dataset_runs{{status="{s}"}} {run_counts.get(s, 0)}' for s in statuses]
    queue_depth = job_counts.get(JobStatus.QUEUED, 0) + run_counts.get(JobStatus.QUEUED, 0)
    lines += [
        "# HELP tracetensor_queue_depth Items waiting for a worker to claim them.",
        "# TYPE tracetensor_queue_depth gauge",
        f"tracetensor_queue_depth {queue_depth}",
        "# HELP tracetensor_worker_embedded Whether this instance runs an embedded worker.",
        "# TYPE tracetensor_worker_embedded gauge",
        f"tracetensor_worker_embedded {1 if settings.WORKER_EMBEDDED else 0}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


# Serve the frontend (built single-page UI) if present.
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def index() -> Any:
        index_file = FRONTEND_DIR / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return {"message": "TraceTensor API running. Frontend not built yet."}
