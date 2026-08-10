"""
Access control — an opt-in shared-secret gate and a rate limiter.

Both are deliberately simple and OFF by default so a localhost/dev run needs no
config. Turn them on with env for any networked deployment:

  API_TOKEN=...            require the token on mutating/execute endpoints
  RATE_LIMIT_PER_MINUTE=N  cap job-creating requests per client per minute (default 60)

The threat this closes: without a token, anyone who can reach the port can start
jobs — i.e. spend your real LLM budget — or delete data. The rate limit is a
backstop against a runaway loop doing the same by accident.

Rate-limit backend (M-6):
  Postgres deployment  → DB-backed sliding window, shared across all API instances.
  SQLite (local dev)   → in-memory per-process (correct for single-process dev).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status

from app.core.config import settings


def _presented_token(request: Request) -> str | None:
    """Pull the token from Authorization: Bearer <t>, X-API-Key: <t>, or a
    `?token=` query param (the last is for EventSource/SSE, which can't set
    headers). Header is preferred; the query param is the documented fallback."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    key = request.headers.get("X-API-Key")
    if key:
        return key.strip()
    qp = request.query_params.get("token")
    return qp.strip() if qp else None


async def require_auth(request: Request) -> None:
    """FastAPI dependency. No-op when API_TOKEN is unset (dev); otherwise the
    request must present the matching token."""
    expected = settings.API_TOKEN
    if not expected:
        return
    presented = _presented_token(request)
    if not presented or not _const_eq(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _const_eq(a: str, b: str) -> bool:
    """Constant-time-ish compare so a token can't be guessed by timing."""
    import hmac

    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# In-memory limiter — used for SQLite / local dev (single-process, correct).
# ---------------------------------------------------------------------------


class _MemoryRateLimiter:
    """Sliding-window limiter backed by an in-process dict.

    Per-instance only: correct for local dev (single process). For a
    multi-instance Postgres deployment use _db_rate_check() instead.
    """

    # How often to sweep the whole dict for keys whose window has fully expired.
    _SWEEP_EVERY_S = 60.0

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._next_sweep = time.monotonic() + self._SWEEP_EVERY_S

    def _sweep(self, cutoff: float) -> None:
        """Drop keys with no hits left in the window.

        Per-key cleanup in check() only fires when that client comes back, so a
        server seeing many one-shot clients (scanners, a wide NAT range) would
        otherwise retain one entry per IP for its whole lifetime. This bounds the
        dict by *active* clients rather than by every client ever seen.
        """
        for k in [k for k, w in self._hits.items() if not w or w[-1] < cutoff]:
            del self._hits[k]

    def check(self, key: str) -> None:
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        cutoff = now - 60.0
        if now >= self._next_sweep:
            self._sweep(cutoff)
            self._next_sweep = now + self._SWEEP_EVERY_S
        window = self._hits[key]
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self.per_minute:
            retry = max(1, int(60 - (now - window[0])))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded ({self.per_minute}/min). Retry in ~{retry}s.",
                headers={"Retry-After": str(retry)},
            )
        window.append(now)


_memory_limiter = _MemoryRateLimiter(settings.RATE_LIMIT_PER_MINUTE)


# ---------------------------------------------------------------------------
# DB-backed limiter — used for Postgres (M-6: shared across all instances).
# ---------------------------------------------------------------------------


async def _db_rate_check(key: str, per_minute: int) -> None:
    """Postgres sliding-window check using _rate_limit_hits as shared state.

    An advisory lock per client key serializes concurrent requests from the
    same client, preventing a burst of parallel requests from all passing the
    count check before any of them insert."""
    from sqlalchemy import text

    from app.core.database import AsyncSessionLocal

    lock_id = hash(key) & 0x7FFFFFFFFFFFFFFF
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})
        await db.execute(
            text("DELETE FROM _rate_limit_hits WHERE key = :k AND hit_at < :c"),
            {"k": key, "c": cutoff},
        )
        count = (
            await db.execute(
                text("SELECT COUNT(*) FROM _rate_limit_hits WHERE key = :k"),
                {"k": key},
            )
        ).scalar_one()
        if count >= per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded ({per_minute}/min). Retry in ~60s.",
                headers={"Retry-After": "60"},
            )
        await db.execute(
            text("INSERT INTO _rate_limit_hits (key, hit_at) VALUES (:k, :now)"),
            {"k": key, "now": datetime.now(timezone.utc)},
        )
        await db.commit()


# ---------------------------------------------------------------------------
# FastAPI dependency — dispatches to the right backend.
# ---------------------------------------------------------------------------


async def rate_limit_jobs(request: Request) -> None:
    """Throttle job/dataset-run creation per client.

    Uses the DB (Postgres) when available so the limit is shared across all API
    instances. Falls back to in-memory for SQLite (local dev / CI).
    """
    if settings.RATE_LIMIT_PER_MINUTE <= 0:
        return
    key = _presented_token(request) or (request.client.host if request.client else "anon")
    if settings.DATABASE_URL.startswith("sqlite"):
        _memory_limiter.check(key)
    else:
        await _db_rate_check(key, settings.RATE_LIMIT_PER_MINUTE)
