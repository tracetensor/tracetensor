"""
Task ORM model — the persisted Patient Registry record.

One row per registered coding-agent task. Stores the parsed config, on-disk
location, per-file presence flags, and the registration lifecycle status.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, JSONType
from app.models.enums import TaskStatus


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Identity
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_type: Mapped[str] = mapped_column(String(50), default="coding", index=True)

    # Parsed chart
    schema_version: Mapped[str] = mapped_column(String(10), default="1.3")
    config: Mapped[dict] = mapped_column(JSONType)

    # On-disk location
    task_dir: Mapped[str] = mapped_column(String(500))

    # File presence (drives the UI checklist)
    has_instruction: Mapped[bool] = mapped_column(Boolean, default=False)
    has_dockerfile: Mapped[bool] = mapped_column(Boolean, default=False)
    has_docker_image: Mapped[bool] = mapped_column(Boolean, default=False)
    has_test_script: Mapped[bool] = mapped_column(Boolean, default=False)
    has_solution: Mapped[bool] = mapped_column(Boolean, default=False)

    # Quick-access environment fields
    environment_image: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    environment_os: Mapped[str] = mapped_column(String(10), default="linux")
    network_mode: Mapped[str] = mapped_column(String(20), default="public")
    timeout_agent_sec: Mapped[float] = mapped_column(Float, default=120.0)
    timeout_verifier_sec: Mapped[float] = mapped_column(Float, default=120.0)

    # Metadata
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Lifecycle: registered -> ready_for_examination
    status: Mapped[str] = mapped_column(String(50), default=TaskStatus.REGISTERED.value, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,  # list ORDER BY
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
