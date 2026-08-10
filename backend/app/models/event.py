"""
Live-progress event log — the durable backing for the SSE streams.

Progress used to live only in a per-process dict, which broke the deployment the
docs actually recommend: with `WORKER_EMBEDDED=false` and workers on other
machines, the worker's events were written into *its* memory and the web tier
serving the browser never saw them. The stream sat empty until it timed out and
fell back to a DB snapshot — no error, just live progress that quietly didn't
work. Multiple web workers had the same problem for the same reason.

Putting the events in a table fixes both, and keeps the property the in-memory
version had and a pub/sub broker would not: the log is append-only and
replayable, so a client that connects late (or reconnects) still gets the full
history before following live.

Rows are pruned once a stream can no longer be watched — see
event_bus.prune_old_events.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, JSONType


class JobEvent(Base):
    __tablename__ = "job_events"
    # Every read is "events for this channel after this id", so the index carries
    # both columns and the query never touches another row.
    __table_args__ = (Index("ix_job_events_channel_id", "channel", "id"),)

    # The monotonic id doubles as the client's cursor: "give me everything after
    # N". A timestamp would be ambiguous under concurrent inserts; a sequence
    # isn't.
    #
    # BIGINT on Postgres, plain INTEGER on SQLite — deliberately, not an
    # oversight. SQLite only auto-increments a column declared exactly
    # `INTEGER PRIMARY KEY` (the rowid alias); a BIGINT primary key gets no
    # value and every insert fails a NOT NULL constraint. Since SQLite is the
    # default dev database, that would have broken live progress everywhere
    # except production.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )

    #: The job or dataset-run id this belongs to, as a string — the bus is
    #: deliberately domain-agnostic and used for both.
    channel: Mapped[str] = mapped_column(String(64), nullable=False)

    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
