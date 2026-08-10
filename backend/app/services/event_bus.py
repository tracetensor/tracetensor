"""
Event bus for live examination progress.

Each running job (or dataset run) has a channel: an append-only log of progress
markers that the worker writes to and the SSE endpoint tails. Append-only is the
important property — a client that connects late, or reconnects, replays the
whole history and then follows live, so nothing is missed. Events are small
markers (phase changes, per-command steps), not stdout byte streams.

Two backends, one interface:

  MemoryBackend    a per-process dict. Correct and fast for a single process
                   with no database — the CLI, and most tests.
  DatabaseBackend  rows in `job_events`. Correct across processes AND machines,
                   which is what the server needs.

The server uses the database backend. That fixes a real defect rather than a
hypothetical one: with `WORKER_EMBEDDED=false` and workers on other machines —
the scaling path the docs recommend — the worker's events went into its own
memory and the web tier serving the browser never saw them. Live progress
silently didn't work, and multiple web workers failed the same way.

Why a table and not Postgres LISTEN/NOTIFY: NOTIFY is transient. A client that
connects mid-run would get only what arrives after it subscribed, losing the
replay guarantee above. The table is the log; NOTIFY could later ride on top of
it purely to cut polling latency, without changing this interface.

`emit()` is synchronous because it's called from the worker thread that runs a
trial. Writes are buffered and flushed by a task on the event loop, so a slow
database can never stall an agent.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple, Union

from app.core.logging import get_logger

log = get_logger("tracetensor.event_bus")

#: An async_sessionmaker, or anything that yields an AsyncSession context —
#: kept structural so the CLI and tests can hand in their own.
SessionFactory = Callable[[], Any]

# Keep at most this many channels in memory before evicting. Only the memory
# backend needs a cap; the database backend prunes by age instead.
_MAX_BUSES = 64

# Any of these marks a channel's stream as finished. The bus is domain-agnostic
# (jobs and dataset runs both use it), so it doesn't hardcode one event name.
_TERMINAL_TYPES = {"job_done", "run_done"}


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class Backend(Protocol):
    """What a backend must provide. Declared so the module-level `_backend` has
    a real type instead of `object` — otherwise every call through it is `Any`
    and a backend missing a method would only fail at run time."""

    def open(self, channel: str) -> None: ...

    def emit(self, channel: str, event: dict) -> None: ...

    async def read_from(self, channel: str, cursor: int) -> Tuple[List[dict], int]: ...

    async def is_done(self, channel: str) -> bool: ...

    def clear(self) -> None: ...

    #: Whether a channel has anything to replay. Synchronous for the in-memory
    #: backend, a query for the database one, so callers go through
    #: `has_events()` which awaits either.
    def exists(self, channel: str) -> Union[bool, Awaitable[bool]]: ...


class MemoryBackend:
    """Per-process, in-memory. What the CLI and the offline tests use."""

    def __init__(self) -> None:
        self._events: Dict[str, List[dict]] = {}
        self._done: set = set()
        self._lock = threading.Lock()

    def open(self, channel: str) -> None:
        with self._lock:
            if len(self._events) >= _MAX_BUSES:
                # Finished channels first, oldest first (dicts keep insertion
                # order); then oldest live ones. An unbounded registry is a worse
                # failure than a very old viewer losing its replay history.
                for ch in [c for c in self._events if c in self._done]:
                    del self._events[ch]
                    self._done.discard(ch)
                    if len(self._events) < _MAX_BUSES:
                        break
                while len(self._events) >= _MAX_BUSES:
                    oldest = next(iter(self._events))
                    del self._events[oldest]
                    self._done.discard(oldest)
            self._events[channel] = []

    def exists(self, channel: str) -> bool:
        with self._lock:
            return channel in self._events

    def emit(self, channel: str, event: dict) -> None:
        with self._lock:
            self._events.setdefault(channel, []).append(event)
            if event.get("type") in _TERMINAL_TYPES:
                self._done.add(channel)

    async def read_from(self, channel: str, cursor: int) -> Tuple[List[dict], int]:
        with self._lock:
            events = self._events.get(channel, [])
            return list(events[cursor:]), len(events)

    async def is_done(self, channel: str) -> bool:
        with self._lock:
            return channel in self._done

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._done.clear()


class DatabaseBackend:
    """Rows in `job_events`, readable from any process or machine.

    Writes are buffered: `emit()` appends to an in-process queue and returns
    immediately, and a task on the event loop flushes batches. That keeps the
    worker thread free of database latency, and turns a burst of per-command
    events into one INSERT instead of dozens.

    Reads deliberately do NOT include the unflushed buffer. A buffered event has
    no row id yet, so returning it couldn't advance the cursor, and the next read
    after the flush would deliver it a second time — duplicated steps in the UI.
    The cost is that a stream lags a flush interval behind, which at 100ms is not
    perceptible; the benefit is that the cursor stays a single monotonic sequence.
    """

    #: How long a batch may sit unflushed. Short enough to feel live, long
    #: enough to batch a burst of step events into one statement.
    FLUSH_INTERVAL_S = 0.1

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._pending: List[tuple] = []  # (channel, payload)
        self._lock = threading.Lock()
        self._done_local: set = set()
        self._flusher: Optional[asyncio.Task] = None
        #: The backend this one displaced, restored on stop(). See
        #: use_database_backend.
        self._previous: Optional[Backend] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._flusher = loop.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None
        await self._flush_once()  # don't drop the tail of a finished run
        if self._previous is not None and _backend is self:
            configure_backend(self._previous)
            self._previous = None

    # -- writes -------------------------------------------------------------

    def open(self, channel: str) -> None:
        # Nothing to allocate — the first event creates the channel. Kept so the
        # two backends present the same interface.
        pass

    def emit(self, channel: str, event: dict) -> None:
        with self._lock:
            self._pending.append((channel, event))
            if event.get("type") in _TERMINAL_TYPES:
                self._done_local.add(channel)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL_S)
            try:
                await self._flush_once()
            except Exception:
                # A failed flush must not kill the flusher — that would silently
                # stop all live progress for the rest of the process's life.
                log.exception("event_flush_failed")

    async def _flush_once(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, []
        if not batch:
            return
        from app.models.event import JobEvent

        try:
            async with self._session_factory() as session:
                session.add_all([JobEvent(channel=ch, payload=payload) for ch, payload in batch])
                await session.commit()
        except Exception:
            # Put them back so a transient database blip doesn't lose progress.
            with self._lock:
                self._pending = batch + self._pending
            raise

    # -- reads --------------------------------------------------------------

    async def exists(self, channel: str) -> bool:
        from sqlalchemy import select

        from app.models.event import JobEvent

        with self._lock:
            if any(ch == channel for ch, _ in self._pending):
                return True
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(JobEvent.id).where(JobEvent.channel == channel).limit(1)
                )
            ).first()
        return row is not None

    async def read_from(self, channel: str, cursor: int) -> Tuple[List[dict], int]:
        """Everything after `cursor`, plus the new cursor.

        The cursor is the last row id seen, not an offset — so it stays valid
        even as other channels interleave rows between reads.
        """
        from sqlalchemy import select

        from app.models.event import JobEvent

        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(JobEvent.id, JobEvent.payload)
                        .where(JobEvent.channel == channel, JobEvent.id > cursor)
                        .order_by(JobEvent.id)
                    )
                )
                .tuples()
                .all()
            )
        if not rows:
            return [], cursor
        return [payload for _id, payload in rows], rows[-1][0]

    async def is_done(self, channel: str) -> bool:
        if channel in self._done_local:
            return True
        from sqlalchemy import select

        from app.models.event import JobEvent

        async with self._session_factory() as session:
            rows = (
                (await session.execute(select(JobEvent.payload).where(JobEvent.channel == channel)))
                .scalars()
                .all()
            )
        return any(isinstance(p, dict) and p.get("type") in _TERMINAL_TYPES for p in rows)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._done_local.clear()


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_backend: Backend = MemoryBackend()


def configure_backend(backend: Backend) -> None:
    """Swap the backend. Called once at startup; the default stays in-memory so
    importing this module never requires a database."""
    global _backend
    _backend = backend


def current_backend() -> Backend:
    return _backend


async def use_database_backend(session_factory: SessionFactory) -> DatabaseBackend:
    """Switch to the durable backend and start its flusher.

    The previous backend is remembered and restored by `stop()`, so a clean
    shutdown leaves the module as it was found. That matters beyond tidiness:
    without it, anything that starts the app in-process (the test suite, an
    embedded harness) leaves a database-backed bus installed globally, and later
    in-memory callers read from a table nothing wrote to.
    """
    backend = DatabaseBackend(session_factory)
    backend._previous = _backend
    backend.start(asyncio.get_running_loop())
    configure_backend(backend)
    return backend


async def use_memory_backend() -> None:
    configure_backend(MemoryBackend())


# ---------------------------------------------------------------------------
# Public handle
# ---------------------------------------------------------------------------


class JobBus:
    """A handle on one channel. Thin by design — the backend holds the state, so
    a handle can be created in one process and the events read in another."""

    def __init__(self, channel: str) -> None:
        self.channel = channel

    def emit(self, event: dict) -> None:
        """Record an event. Synchronous and non-blocking: this is called from the
        worker thread running a trial, which must never wait on the database."""
        _backend.emit(self.channel, event)

    async def read_from(self, cursor: int) -> Tuple[List[dict], int]:
        """Return (events after `cursor`, new cursor)."""
        return await _backend.read_from(self.channel, cursor)

    async def is_done(self) -> bool:
        return await _backend.is_done(self.channel)


def create_bus(job_id: str) -> JobBus:
    """Open a channel and return a handle to it."""
    _backend.open(job_id)
    return JobBus(job_id)


def get_bus(job_id: str) -> JobBus:
    """A handle on a channel. Always returns one — whether it has any events is
    a question for `read_from`, and with a shared backend the answer can change
    between processes."""
    return JobBus(job_id)


async def has_events(job_id: str) -> bool:
    """Whether this channel has anything to replay.

    The stream endpoints use this to decide between tailing and reconstructing a
    terminal snapshot from the database.
    """
    result = _backend.exists(job_id)
    if isinstance(result, bool):
        return result
    return await result


def clear_all_buses() -> None:
    """Drop all channel state. For tests — the backend is process-global, so
    without this one test's events leak into the next and order starts to
    matter."""
    _backend.clear()


async def prune_old_events(session_factory: SessionFactory, older_than_seconds: int) -> int:
    """Delete events past the window in which a stream could still be watched.

    Without this the log grows forever: a dataset run over 100 tasks emits
    thousands of markers. The window is generous (a stream self-terminates well
    before it) but bounded.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from app.models.event import JobEvent

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    async with session_factory() as session:
        result = await session.execute(delete(JobEvent).where(JobEvent.created_at < cutoff))
        await session.commit()
    return result.rowcount or 0
