"""Async SQLAlchemy engine, session factory, and Base.

Schema ownership: **Alembic, always.** `init_db()` runs `alembic upgrade head`;
nothing here issues DDL of its own. There used to be a second owner — an
`_ensure_additive_columns()` shim that ran raw ALTER/CREATE INDEX on every
startup — which meant a contributor writing a proper migration would collide
with it in production. Everything that shim did now lives in revision
0003_schema_consolidation. If you change a model, add a revision; there is no
other path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

if TYPE_CHECKING:  # import-cycle-free typing
    from alembic.config import Config

# Portable JSON column type. Postgres gets JSONB (indexable, efficient); SQLite
# — used for keyless local dev / offline tests — falls back to plain JSON, which
# it can render (JSONB can't compile on SQLite). Postgres UUID already degrades
# gracefully on SQLite, so JSON is the only type that needs a variant.
JSONType = JSONB().with_variant(JSON(), "sqlite")

# Pool config applies to Postgres; SQLite (dev) uses its own defaults. pool_pre_ping
# validates a connection before use so a dropped/stale conn doesn't fail a request.
_engine_kwargs: dict[str, object] = {"echo": False, "future": True, "pool_pre_ping": True}
if not settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs.update(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
    )

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

# backend/ — where alembic.ini and alembic/ live. Resolved from this file rather
# than the process cwd so migrations run correctly no matter where the server was
# started from (tests launch it from several different directories).
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a database session."""
    async with AsyncSessionLocal() as session:
        yield session


def import_all_models() -> None:
    """Import every model module so all tables register on Base.metadata.

    Alembic's env.py needs this and so does anything that reflects on the
    metadata; keeping one list means a new model can't be half-registered.
    """
    from app.models import dataset as _dataset  # noqa: F401
    from app.models import event as _event  # noqa: F401
    from app.models import job as _job  # noqa: F401
    from app.models import rate_limit as _rate_limit  # noqa: F401
    from app.models import task as _task  # noqa: F401


def alembic_config() -> "Config":
    """A programmatic Alembic Config pointing at backend/alembic.

    Built without the .ini file on purpose: loading it would re-run
    `fileConfig()` and stomp the app's configured logging on every startup.
    Only script_location and the URL matter for `upgrade`.
    """
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return cfg


_MIGRATION_LOCK_ID = 0x54542D4D494752  # "TT-MIGR" as a 64-bit advisory lock key


def _upgrade_to_head() -> None:
    """Blocking `alembic upgrade head`. Runs in a worker thread — alembic's
    env.py calls asyncio.run(), which cannot start inside a running loop.

    A Postgres advisory lock prevents multiple pods starting simultaneously
    from racing on the same migration. The lock is session-scoped and released
    when the connection closes (the `with` block)."""
    from alembic import command

    if settings.DATABASE_URL.startswith("sqlite"):
        command.upgrade(alembic_config(), "head")
        return

    from sqlalchemy import create_engine, text

    sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
    sync_engine = create_engine(sync_url)
    with sync_engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _MIGRATION_LOCK_ID})
        try:
            command.upgrade(alembic_config(), "head")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _MIGRATION_LOCK_ID})
    sync_engine.dispose()


async def init_db() -> None:
    """Bring the database to the current schema.

    Idempotent and safe to call on every boot: on an up-to-date database Alembic
    compares the version table and does nothing.
    """
    import_all_models()
    await asyncio.to_thread(_upgrade_to_head)
