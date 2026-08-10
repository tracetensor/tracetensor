"""
Rate-limit hit log — shared state for the DB-backed limiter.

Not a domain entity (hence the leading underscore on the table name): it's an
internal ledger of "client X made a request at time T", used only by
app.core.security._db_rate_check. It lives here as a real ORM model rather than
as raw startup DDL so exactly one system — the model metadata, via Alembic —
defines the schema.

Only the Postgres deployment reads/writes it; SQLite dev uses the in-process
limiter. The table is still created on SQLite so both dialects have an identical
schema, which is what the schema-parity test asserts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RateLimitHit(Base):
    __tablename__ = "_rate_limit_hits"
    # Every query is "hits for this key inside this window" — a composite index
    # on exactly that keeps both the count and the prune cheap.
    __table_args__ = (Index("ix_rate_limit_hits_key_hit_at", "key", "hit_at"),)

    # Surrogate PK: the limiter never addresses a row individually, but SQLAlchemy
    # requires a primary key and a real one keeps the table diffable by Alembic.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    hit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
