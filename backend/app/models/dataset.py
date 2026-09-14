"""
Dataset ORM models — the Patient Cohort.

A DATASET is a named, versioned bundle of tasks you can run in one command
(TraceTensor's equivalent of curated benchmark suites like `swebench-verified==1.0`). A DATASET RUN
is one examination order for a whole cohort: "run this agent+model across every
task, N trials each." Each task in a run gets its own Job (reusing Stage 2), and
all the trials fan out through a single global concurrency cap.

Doctor analogy:
  Dataset     = a named patient cohort (frozen at a version)
  DatasetRun  = admit the whole cohort and examine them together
  (each patient's examination is still a Job + its Trials)

Note: annotations use typing.Optional / typing.List so the ORM resolves them on
Python 3.9, matching the rest of app/.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, JSONType
from app.models.enums import JobStatus


class Dataset(Base):
    __tablename__ = "datasets"
    # (name, version) is immutable and unique — enforced at the DB level, not
    # just in app code, so concurrent uploads can't create duplicates.
    __table_args__ = (Index("uq_datasets_name_version", "name", "version", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[str] = mapped_column(String(50))

    # Stable hash of every task's files — lets us enforce that a given
    # (name, version) is immutable: re-uploading identical content is a no-op,
    # re-uploading different content under the same version is rejected.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    # Manifest order preserved. task_ids parallels task_names.
    task_ids: Mapped[list] = mapped_column(JSONType, default=list)
    task_names: Mapped[list] = mapped_column(JSONType, default=list)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,  # list ORDER BY
    )


class DatasetRun(Base):
    __tablename__ = "dataset_runs"
    __table_args__ = (
        Index("uq_dataset_runs_idempotency_key", "idempotency_key", unique=True),
        # The leaderboard: completed runs for one dataset, newest first. Without
        # this it's a scan of every run the dataset ever had.
        Index("ix_dataset_runs_dataset_status_created", "dataset_id", "status", "created_at"),
        # Same queue-claim and lease-reclaim paths as Job.
        Index("ix_dataset_runs_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id"), index=True
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    agent: Mapped[str] = mapped_column(String(50))
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    n_trials: Mapped[int] = mapped_column(Integer, default=1)
    concurrency: Mapped[int] = mapped_column(Integer, default=4)
    backend: Mapped[str] = mapped_column(String(40), default="docker")

    # queued -> running -> completed | failed
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED.value, index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ---- Queue lease (durable job queue; see Job for the mechanism) ----------
    worker_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,  # list ORDER BY
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
