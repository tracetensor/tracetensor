"""
Job worker — claims queued work from the durable queue and runs it.

Runs in either of two shapes, same loop:
  - EMBEDDED: a daemon thread inside the API process (default). Single-node
    deployments need zero extra moving parts — the API also executes jobs.
  - STANDALONE: `python -m app.worker`, one process per worker, as many as you
    like across as many machines as you like, all pointed at the same Postgres.
    Set WORKER_EMBEDDED=false on the API tier and scale these independently.

The loop keeps up to WORKER_CONCURRENCY items running at once, claiming more as
slots free, and periodically reclaims leases from workers that died so nothing
wedges in 'running' forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.services import event_bus, job_queue
from app.services.executor import execute_job, make_worker_engine

log = get_logger("tracetensor.worker")


async def _reclaim_once(SessionLocal: async_sessionmaker) -> None:
    counts = await job_queue.reclaim_stale(SessionLocal, settings.WORKER_LEASE_TIMEOUT)
    if counts["jobs"] or counts["dataset_runs"]:
        log.warning(
            "reclaimed_stale_leases",
            extra={"jobs": counts["jobs"], "dataset_runs": counts["dataset_runs"]},
        )

    # Prune the live-progress log on the same sweep. Startup prunes too, but a
    # server that stays up for months would otherwise never clean it, and a
    # dataset run over 100 tasks emits thousands of markers.
    try:
        pruned = await event_bus.prune_old_events(SessionLocal, settings.EVENT_RETENTION_SECONDS)
        if pruned:
            log.info("event_log_pruned", extra={"rows": pruned})
    except Exception:
        # Housekeeping must never take the worker down with it.
        log.exception("event_prune_failed")


async def worker_loop(engine: AsyncEngine, worker_id: str, stop: threading.Event) -> None:
    """Claim → run, keeping up to WORKER_CONCURRENCY items in flight, until
    `stop` is set. `stop` is a threading.Event so it can be tripped from another
    thread (the API's shutdown) or a signal handler; the loop notices it within
    one poll interval, stops claiming NEW work, and drains what's in flight."""
    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)
    running: set[asyncio.Task] = set()
    log.info(
        "worker_started", extra={"worker_id": worker_id, "concurrency": settings.WORKER_CONCURRENCY}
    )

    # Clean up anything a previous run left leased before we start pulling work.
    await _reclaim_once(SessionLocal)
    # Sweep for dead leases twice per lease window: often enough that a crashed
    # worker's jobs are reclaimed within one lease rather than two, with a 5s
    # floor so a very short lease can't turn this into a busy loop.
    reclaim_every = max(5.0, settings.WORKER_LEASE_TIMEOUT / 2.0)
    since_reclaim = 0.0

    while not stop.is_set():
        claimed_any = False
        # Fill free slots by claiming jobs (standalone or dataset-run children —
        # a cohort's tasks spread across every worker this way).
        while len(running) < settings.WORKER_CONCURRENCY and not stop.is_set():
            async with SessionLocal() as db:
                item_id = await job_queue.claim_job(db, worker_id)
            if not item_id:
                break
            log.info("claimed", extra={"id": str(item_id), "worker_id": worker_id})
            task = asyncio.create_task(execute_job(SessionLocal, item_id))
            running.add(task)
            task.add_done_callback(running.discard)
            claimed_any = True

        if not claimed_any:
            await asyncio.sleep(settings.WORKER_POLL_INTERVAL)
            since_reclaim += settings.WORKER_POLL_INTERVAL
            if since_reclaim >= reclaim_every:
                await _reclaim_once(SessionLocal)
                since_reclaim = 0.0

    # Graceful drain: let in-flight items finish before exiting.
    if running:
        log.info("worker_draining", extra={"in_flight": len(running)})
        await asyncio.gather(*running, return_exceptions=True)
    log.info("worker_stopped", extra={"worker_id": worker_id})


class EmbeddedWorker:
    """Handle for the in-process worker so the API can drain it on shutdown."""

    def __init__(self, thread: threading.Thread, stop: threading.Event) -> None:
        self._thread = thread
        self._stop = stop

    def shutdown(self, timeout: float) -> bool:
        """Signal the worker to stop claiming and drain in-flight jobs; wait up
        to `timeout`. Returns True if it drained cleanly, False if it timed out
        (in-flight jobs then get reclaimed by another worker's lease sweep)."""
        self._stop.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()


def start_embedded_worker() -> EmbeddedWorker:
    """Start the in-process worker as a daemon thread (called from app startup).

    Its own thread + event loop + NullPool engine, because asyncpg connections
    are bound to the loop that created them and must not touch the API's engine.
    """
    stop = threading.Event()

    def _run() -> None:
        engine = make_worker_engine()

        async def _main() -> None:
            try:
                await worker_loop(engine, job_queue.worker_identity(), stop)
            finally:
                await engine.dispose()

        asyncio.run(_main())

    thread = threading.Thread(target=_run, daemon=True, name="tt-embedded-worker")
    thread.start()
    return EmbeddedWorker(thread, stop)


def main() -> None:
    """Standalone worker entrypoint: `python -m app.worker`."""
    configure_logging()
    engine = make_worker_engine()

    async def _main() -> None:
        stop = threading.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await worker_loop(engine, job_queue.worker_identity(), stop)
        finally:
            await engine.dispose()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
