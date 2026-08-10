"""
Helpers shared by the routers.

Each of these existed twice — once in examine.py and once in datasets.py, or in
ingest.py and datasets.py — with the two copies drifting apart over time. The SSE
timeout, for instance, was fixed in one stream endpoint and not the other. One
home each, so the next fix lands everywhere.

Nothing here holds business logic; it's the HTTP-shaped plumbing that two
endpoints happen to need. Domain logic belongs in app/services/.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, AsyncIterator, Optional

from fastapi import HTTPException, Request, UploadFile

from app.core.config import settings

if TYPE_CHECKING:  # avoids a router → service import at module load
    from app.services.event_bus import JobBus

# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

# Caps on compressed upload size, applied BEFORE the bytes are read into memory.
# The uncompressed limit is enforced separately inside extract_zip_files; this
# one stops a huge payload or zip bomb from reaching the decompressor at all.
MAX_TASK_ZIP_BYTES = 50 * 1024 * 1024  # 50 MB — one task
MAX_DATASET_ZIP_BYTES = 200 * 1024 * 1024  # 200 MB — a bundle of many tasks
MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024  # 10 MB — one file in a multipart form


async def read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload with a hard size cap. Raises 413 if exceeded.

    Reads max_bytes + 1 rather than checking a declared Content-Length: the
    header is client-supplied and a lie is exactly what this guards against.
    """
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Upload too large (max {max_bytes // 1024 // 1024} MB).",
        )
    return data


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

MAX_TRIALS = 50
MAX_CONCURRENCY = 16
DEFAULT_CONCURRENCY = 4


def validate_trial_params(n_trials: int, concurrency: Optional[int], ceiling: int) -> int:
    """Check the trial fan-out a request asked for and return the effective width.

    `ceiling` is what the caller can actually run in parallel: the trial count for
    a single task (more workers than trials is waste), or MAX_CONCURRENCY for a
    dataset run, where trials from different tasks fill the slots. The two
    endpoints genuinely differ here, so it's a parameter rather than a divergence.
    """
    if n_trials < 1 or n_trials > MAX_TRIALS:
        raise HTTPException(status_code=400, detail=f"n_trials must be 1–{MAX_TRIALS}.")
    if concurrency is not None and (concurrency < 1 or concurrency > MAX_CONCURRENCY):
        raise HTTPException(status_code=400, detail=f"concurrency must be 1–{MAX_CONCURRENCY}.")
    return min(concurrency or DEFAULT_CONCURRENCY, ceiling)


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------


def sse(evt: dict) -> str:
    """Format one event in the SSE wire format."""
    return f"data: {json.dumps(evt)}\n\n"


# How long a stream will follow a live job before giving up. Twice the lease
# timeout: past that the worker is presumed dead and its lease is being
# reclaimed, so there will never be another event. Without this the connection
# hangs open forever on a crashed worker and the browser shows a spinner that
# nothing will ever resolve.
def stream_deadline() -> float:
    return time.monotonic() + settings.WORKER_LEASE_TIMEOUT * 2


def should_tail(status: str, has_events: bool) -> bool:
    """Whether a stream should follow live progress or send a terminal snapshot.

    Keyed off the item's STATUS first, not off whether the event log has rows.
    A job queued milliseconds ago has no events yet — asking the log alone would
    send a snapshot of nothing and close the stream before the worker emitted
    anything, so the browser would show a run that never appears to start.

    A finished item still tails when it has events, so a client reconnecting to
    a run that just completed replays the whole history rather than jumping to
    the summary.
    """
    from app.models.enums import JobStatus

    return status not in JobStatus.terminal() or has_events


async def tail_bus(
    bus: "JobBus", request: Request, poll_interval: float = 0.15
) -> AsyncIterator[str]:
    """Replay a bus's history, then follow it live until the work is done.

    Ends on any of: the client disconnecting, the bus reaching a terminal event,
    or the deadline passing (which yields an error event first, so the client
    learns the stream died rather than just going quiet).
    """
    deadline = stream_deadline()
    cursor = 0
    while True:
        if await request.is_disconnected():
            return
        if time.monotonic() > deadline:
            yield sse({"type": "error", "detail": "stream timed out — worker may have crashed"})
            return
        events, cursor = await bus.read_from(cursor)
        for evt in events:
            yield sse(evt)
        if not events and await bus.is_done():
            return
        await asyncio.sleep(poll_interval)
