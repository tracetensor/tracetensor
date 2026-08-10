"""API schemas for datasets (Patient Cohorts).

typing.Optional / typing.List (not `X | None`) so Pydantic resolves annotations
on Python 3.9, matching the rest of app/schemas.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

from app.schemas.common import JobStatusLiteral, TaskStatusLiteral


class DatasetTaskInfo(BaseModel):
    task_id: uuid.UUID
    name: str
    # A TASK's registration lifecycle, not a job's run status — a task in a
    # dataset can be `registered` (stored but not runnable) and dataset runs
    # skip it. Different state machine, different type.
    status: TaskStatusLiteral


class DatasetView(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    description: Optional[str]
    content_hash: str
    task_count: int
    tasks: List[DatasetTaskInfo]
    created_at: datetime


class DatasetSummary(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    task_count: int
    created_at: datetime


class DatasetRunRequest(BaseModel):
    agent: str = "anthropic"
    model: Optional[str] = None
    n_trials: int = 1
    # Global cap across the WHOLE run (all tasks × trials), not per task.
    concurrency: Optional[int] = None


class TaskScore(BaseModel):
    task_id: uuid.UUID
    task_name: str
    job_id: uuid.UUID
    status: JobStatusLiteral
    n_trials: int
    trials_completed: int
    trials_passed: int
    pass_rate: Optional[float]


class DatasetRunView(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    dataset_name: str
    dataset_version: str
    agent: str
    model: Optional[str]
    n_trials: int
    concurrency: int
    status: JobStatusLiteral
    error: Optional[str]
    created_at: datetime
    finished_at: Optional[datetime]
    # Scoreboard.
    task_count: int
    tasks_completed: int
    overall_pass_rate: Optional[float]  # mean of per-task pass rates
    scores: List[TaskScore]


class DatasetRunSummary(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    agent: str
    model: Optional[str]
    n_trials: int
    status: JobStatusLiteral
    overall_pass_rate: Optional[float]
    created_at: datetime


# ---- Aggregate leaderboard (agent × model × task) ----
class LeaderboardTask(BaseModel):
    task_id: uuid.UUID
    name: str


class LeaderboardCell(BaseModel):
    task_id: uuid.UUID
    pass_rate: Optional[float]
    job_id: Optional[uuid.UUID]  # to drill into the trajectory
    # None when this (agent, model) never ran the task — a blank cell, which is
    # not the same thing as a task the agent failed.
    status: Optional[JobStatusLiteral]


class LeaderboardRow(BaseModel):
    agent: str
    model: Optional[str]
    run_id: uuid.UUID  # the representative (latest) run
    run_count: int  # how many completed runs exist for this combo
    overall_pass_rate: Optional[float]
    created_at: datetime
    cells: List[LeaderboardCell]  # aligned to LeaderboardView.tasks order


class LeaderboardView(BaseModel):
    dataset_id: uuid.UUID
    dataset_name: str
    version: str
    tasks: List[LeaderboardTask]  # column order
    rows: List[LeaderboardRow]  # one per agent+model, sorted by overall desc
