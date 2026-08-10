"""
Evaluation ingestion endpoints.

Two ways to register an evaluation (standard task folder):
  1. POST /ingest/task/upload   — ZIP of an evaluation directory
  2. POST /ingest/task/create   — upload the individual files via a form

Layout on disk: instruction.md, task.toml, environment/, tests/, solution/ (optional).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import rate_limit_jobs
from app.models.task import Task
from app.routers._shared import (
    MAX_SINGLE_FILE_BYTES,
    MAX_TASK_ZIP_BYTES,
    read_upload,
)
from app.schemas.api import (
    ExaminationRoom,
    HealthChecks,
    PatientChart,
    TaskFilesResponse,
    TaskListResponse,
    TaskRegistration,
    TaskSummary,
    ValidationInfo,
)
from app.schemas.common import ERROR_RESPONSES, as_task_status
from app.services.task_parser import parse_task_toml
from app.services.task_service import (
    EXAMPLES_DIR,
    TaskParseError,
    default_task_toml,
    gather_files,
    persist_task,
)
from app.services.task_validator import STATUS_READY, ValidationResult, validate_task
from app.storage.task_store import (
    read_task_file,
    store_task_from_files,
    store_task_from_zip,
)

router = APIRouter(prefix="/ingest", tags=["Evaluations"])


async def _register(db: AsyncSession, task_dir: Path) -> tuple[Task, ValidationResult]:
    """persist_task + the one HTTP translation the routers owe it."""
    try:
        return await persist_task(db, task_dir)
    except TaskParseError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def _build_registration(task: Task, validation: ValidationResult) -> TaskRegistration:
    errors = validation.errors
    warnings = validation.warnings

    ready = task.status == STATUS_READY
    if ready:
        message = "Evaluation registered and ready to run."
        next_step = f"POST /examine/{task.id} to start a run."
    else:
        message = "Evaluation registered, but it is incomplete. See validation errors."
        next_step = "Fix the reported errors and re-upload."

    cfg = task.config or {}
    return TaskRegistration(
        id=task.id,
        status=as_task_status(task.status),
        message=message,
        patient_chart=PatientChart(
            name=task.name,
            type=task.task_type,
            description=task.description,
            category=task.category,
            authors=cfg.get("authors", []),
            keywords=cfg.get("keywords", []),
        ),
        examination_room=ExaminationRoom(
            environment=task.environment_image or "Dockerfile",
            os=task.environment_os,
            network_mode=task.network_mode,
            agent_timeout_sec=task.timeout_agent_sec,
            verifier_timeout_sec=task.timeout_verifier_sec,
        ),
        health_checks=HealthChecks(
            has_instruction=task.has_instruction,
            has_task_toml=(task.config is not None),
            has_dockerfile=task.has_dockerfile,
            has_docker_image=task.has_docker_image,
            has_test_script=task.has_test_script,
            has_solution=task.has_solution,
        ),
        validation=ValidationInfo(errors=errors, warnings=warnings),
        next_step=next_step,
        created_at=task.created_at,
    )


@router.post(
    "/task/upload",
    response_model=TaskRegistration,
    responses=ERROR_RESPONSES,
    dependencies=[Depends(rate_limit_jobs)],
)
async def register_task_from_zip(
    file: UploadFile = File(..., description="ZIP of an evaluation directory"),
    db: AsyncSession = Depends(get_db),
) -> TaskRegistration:
    """Register an evaluation by uploading a zipped task directory."""
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Please upload a .zip file.")

    zip_bytes = await read_upload(file, MAX_TASK_ZIP_BYTES)

    task_name = Path(file.filename).stem
    try:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for m in zf.namelist():
                if m.endswith("task.toml"):
                    p = Path(m).parent
                    hint = p if p.name and p.name not in (".", "") else None
                    cfg = parse_task_toml(zf.read(m), hint)
                    task_name = cfg.name
                    break
    except (TaskParseError, KeyError, zipfile.BadZipFile):
        pass  # best-effort name extraction; real validation happens in store_task_from_zip

    try:
        task_dir = store_task_from_zip(settings.TASKS_ROOT, task_name, zip_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Bad archive: {e}")

    if not (task_dir / "task.toml").exists():
        (task_dir / "task.toml").write_bytes(default_task_toml(task_name))

    task, validation = await _register(db, task_dir)
    return _build_registration(task, validation)


@router.post(
    "/task/create",
    response_model=TaskRegistration,
    responses=ERROR_RESPONSES,
    dependencies=[Depends(rate_limit_jobs)],
)
async def register_task_from_files(
    name: str = Form(..., description="Evaluation name, e.g. myorg/sort-csv"),
    instruction: UploadFile = File(..., description="instruction.md"),
    dockerfile: UploadFile = File(..., description="environment/Dockerfile"),
    test_script: UploadFile = File(..., description="tests/test.sh"),
    solution: Optional[UploadFile] = File(None, description="solution/solve.sh (optional)"),
    task_config: Optional[UploadFile] = File(None, description="task.toml"),
    db: AsyncSession = Depends(get_db),
) -> TaskRegistration:
    """Register an evaluation by uploading its task files individually."""
    files: dict[str, bytes] = {
        "instruction.md": await read_upload(instruction, MAX_SINGLE_FILE_BYTES),
        "environment/Dockerfile": await read_upload(dockerfile, MAX_SINGLE_FILE_BYTES),
        "tests/test.sh": await read_upload(test_script, MAX_SINGLE_FILE_BYTES),
    }
    if solution is not None:
        files["solution/solve.sh"] = await read_upload(solution, MAX_SINGLE_FILE_BYTES)
    if task_config is not None:
        files["task.toml"] = await read_upload(task_config, MAX_SINGLE_FILE_BYTES)
    else:
        files["task.toml"] = default_task_toml(name)

    task_dir = store_task_from_files(settings.TASKS_ROOT, name, files)
    task, validation = await _register(db, task_dir)
    return _build_registration(task, validation)


@router.post("/example", response_model=TaskRegistration, responses=ERROR_RESPONSES)
async def register_example_task(db: AsyncSession = Depends(get_db)) -> TaskRegistration:
    """One-click: register the bundled example evaluation (examples/sort-csv)."""
    src = EXAMPLES_DIR / "sort-csv"
    if not (src / "task.toml").exists():
        raise HTTPException(status_code=404, detail="Example evaluation not found on the server.")
    files = gather_files(src)
    try:
        name = parse_task_toml(files["task.toml"], src).name
    except TaskParseError:
        name = "tracetensor/sort-csv"
    task_dir = store_task_from_files(settings.TASKS_ROOT, name, files)
    task, validation = await _register(db, task_dir)
    return _build_registration(task, validation)


@router.get("/tasks", response_model=TaskListResponse, responses=ERROR_RESPONSES)
async def list_tasks(
    task_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> TaskListResponse:
    """List all registered evaluations."""
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be 1–1000.")

    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
    if task_type:
        stmt = stmt.where(Task.task_type == task_type)
    if status:
        stmt = stmt.where(Task.status == status)

    rows = (await db.execute(stmt)).scalars().all()
    return TaskListResponse(
        count=len(rows),
        tasks=[
            TaskSummary(
                id=t.id,
                name=t.name,
                description=t.description,
                task_type=t.task_type,
                status=as_task_status(t.status),
                category=t.category,
                environment_os=t.environment_os,
                created_at=t.created_at,
            )
            for t in rows
        ],
    )


@router.get("/task/{task_id}", response_model=TaskRegistration, responses=ERROR_RESPONSES)
async def get_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> TaskRegistration:
    """Fetch a single evaluation's registration + re-run validation."""
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Evaluation not found.")

    return _build_registration(task, validate_task(Path(task.task_dir)))


@router.get("/task/{task_id}/files", response_model=TaskFilesResponse, responses=ERROR_RESPONSES)
async def get_task_files(
    task_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> TaskFilesResponse:
    """Return the raw file contents for the detail view."""
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Evaluation not found.")

    d = Path(task.task_dir)
    return TaskFilesResponse(
        instruction_md=read_task_file(d, "instruction.md"),
        task_toml=read_task_file(d, "task.toml"),
        dockerfile=read_task_file(d, "environment/Dockerfile"),
        test_sh=read_task_file(d, "tests/test.sh"),
        solve_sh=read_task_file(d, "solution/solve.sh"),
    )
