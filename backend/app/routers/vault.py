"""
Vault API — Phase 1 local browse/export over examination jobs.

List summaries live on GET /examine/jobs (enriched with duration/tokens).
This router adds a Vault-named surface + portable export download.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.job import Job
from app.routers._views import _job_summary, _job_view
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.examine import JobSummary
from app.services import job_service

router = APIRouter(prefix="/vault", tags=["Vault"])


@router.get("/jobs", response_model=Page[JobSummary], responses=ERROR_RESPONSES)
async def vault_list_jobs(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[JobSummary]:
    """Same enriched job list as /examine/jobs — Vault product entrypoint."""
    jobs, names, total = await job_service.list_jobs(db, limit=limit, offset=offset)
    return Page.of([_job_summary(j, names.get(j.task_id)) for j in jobs], total, limit, offset)


@router.get("/export/{job_id}", responses=ERROR_RESPONSES)
async def vault_export_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Any:
    """Download a portable JSON archive for one job (config + trials + trajectories)."""
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    view = await _job_view(db, job)
    payload = view.model_dump(mode="json")
    body = json.dumps(payload, indent=2, default=str)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="job-{job_id}.json"',
        },
    )
