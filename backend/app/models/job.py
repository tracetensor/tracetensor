"""
Stage 2 ORM models — the Examination Room's records.

A JOB is one examination order for a task: "run this patient through N trials."
A TRIAL is one attempt: build the room, let the agent work, run the health
check, record what happened and the score.

Doctor analogy:
  Job   = the examination order (run the patient N times)
  Trial = a single examination session with its recorded chart + result

Note: annotations use typing.Optional / typing.List (not `X | None`) so the
ORM's annotation resolution works on Python 3.9, matching the rest of app/.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, JSONType
from app.models.enums import JobStatus, TrialStatus


class Job(Base):
    __tablename__ = "jobs"
    # Unique idempotency key (NULL for most jobs) — a retried POST with the same
    # Idempotency-Key returns the existing job instead of starting a second run.
    __table_args__ = (
        Index("uq_jobs_idempotency_key", "idempotency_key", unique=True),
        # The queue claim: "oldest queued job". Single-column indexes on status
        # and created_at each answer half of it and leave the database sorting
        # the rest — this is the hot path every worker hits in a poll loop.
        Index("ix_jobs_status_created_at", "status", "created_at"),
        # Reclaiming dead leases: "running, and the heartbeat went stale".
        Index("ix_jobs_status_heartbeat_at", "status", "heartbeat_at"),
        # The per-task job list, newest first.
        Index("ix_jobs_task_id_created_at", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), index=True
    )
    # Set when this job is part of a dataset (cohort) run; NULL for a plain
    # single-task examination. Groups the per-task jobs of one dataset run.
    dataset_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_runs.id"), nullable=True, index=True
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    # Which agent attempts the task, and (optionally) which model.
    agent: Mapped[str] = mapped_column(String(50), default="oracle")
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    n_trials: Mapped[int] = mapped_column(Integer, default=1)
    # Trial fan-out width for this job — stored so a worker that CLAIMS the job
    # off the queue can reconstruct the run without the original request.
    concurrency: Mapped[int] = mapped_column(Integer, default=4)

    # queued -> running -> completed | failed
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED.value, index=True)

    # ---- Queue lease (durable job queue) ------------------------------------
    # A worker claims a queued job by stamping these; a stale heartbeat means the
    # worker died and the lease can be reclaimed. NULL until claimed.
    worker_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Rollup once finished.
    trials_completed: Mapped[int] = mapped_column(Integer, default=0)
    trials_passed: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,  # list ORDER BY
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    trials: Mapped[List["Trial"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Trial(Base):
    __tablename__ = "trials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), index=True
    )

    trial_num: Mapped[int] = mapped_column(Integer, default=0)

    # setup -> agent_running -> verifying -> completed | error
    status: Mapped[str] = mapped_column(String(30), default=TrialStatus.SETUP.value)

    # Result
    reward: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    passed: Mapped[Optional[bool]] = mapped_column(nullable=True)

    # The recorded examination chart: ordered list of steps the agent took,
    # each with command, stdout, stderr, exit_code, and duration.
    trajectory: Mapped[dict] = mapped_column(JSONType, default=dict)

    # Raw verifier output + parsed reward payload.
    verifier_log: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reward_payload: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)

    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job: Mapped["Job"] = relationship(back_populates="trials")
