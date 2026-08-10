"""API request/response schemas (the shapes the frontend consumes)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.schemas.common import TaskStatusLiteral


class HealthChecks(BaseModel):
    has_instruction: bool
    has_task_toml: bool
    has_dockerfile: bool
    has_docker_image: bool
    has_test_script: bool
    has_solution: bool


class ValidationInfo(BaseModel):
    errors: list[str]
    warnings: list[str]


class ExaminationRoom(BaseModel):
    environment: str  # image name or "Dockerfile"
    os: str
    network_mode: str
    agent_timeout_sec: float
    verifier_timeout_sec: float


class PatientChart(BaseModel):
    name: str
    type: str
    description: Optional[str]
    category: Optional[str]
    authors: list[dict]
    keywords: list[str]


class TaskRegistration(BaseModel):
    """Full response after a task is registered."""

    id: uuid.UUID
    status: TaskStatusLiteral
    message: str
    patient_chart: PatientChart
    examination_room: ExaminationRoom
    health_checks: HealthChecks
    validation: ValidationInfo
    next_step: str
    created_at: datetime


class TaskSummary(BaseModel):
    """Compact row for the task list."""

    id: uuid.UUID
    name: str
    description: Optional[str]
    task_type: str
    status: TaskStatusLiteral
    category: Optional[str]
    environment_os: str
    created_at: datetime


class TaskListResponse(BaseModel):
    count: int
    tasks: list[TaskSummary]


class TaskFilesResponse(BaseModel):
    """Raw file contents for the detail view."""

    instruction_md: Optional[str]
    task_toml: Optional[str]
    dockerfile: Optional[str]
    test_sh: Optional[str]
    solve_sh: Optional[str]
