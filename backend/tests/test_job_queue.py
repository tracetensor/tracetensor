"""
Queue-mechanics suite — the claim/reclaim logic that makes the job system
distributable. Pure DB logic: no Docker, no API key, no HTTP.

By default runs against a temp SQLite DB (its serialized-writer path). Point it
at Postgres to exercise the real SELECT ... FOR UPDATE SKIP LOCKED claim:

  TEST_DATABASE_URL=postgresql+asyncpg://tracetensor:tracetensor@localhost:5433/tracetensor \
      python tests/test_job_queue.py

Covers:
  - a queued job is claimed exactly once, even by many workers at once (the core
    guarantee that lets you run workers on many machines safely)
  - a claim flips status queued -> running and stamps the lease
  - reclaim_stale fails a running job whose heartbeat went stale (crash recovery)
  - a fresh lease is NOT reclaimed (a live worker is never stolen from)
  - dataset-run CHILD jobs are claimable (sharding), and the run finalizes
    (once, idempotently) when its last child is terminal — including when that
    last child was just reclaimed after a worker crash
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

_tmp = None
if not os.getenv("TEST_DATABASE_URL"):
    _tmp = tempfile.mkdtemp(prefix="tt_queue_")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/q.db"
else:
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.database import Base  # noqa: E402
from app.models.dataset import Dataset, DatasetRun  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.services import job_queue  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


async def main() -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    dialect = engine.dialect.name
    print(f"== queue mechanics on {dialect} ==")

    # Seed a task + N queued standalone jobs.
    N = 12
    async with Session() as db:
        t = Task(
            id=uuid.uuid4(),
            name="q/task",
            config={},
            task_dir="/tmp/none",
            status="ready_for_examination",
        )
        db.add(t)
        await db.flush()
        for _ in range(N):
            db.add(Job(id=uuid.uuid4(), task_id=t.id, agent="oracle", n_trials=1, status="queued"))
        await db.commit()
        task_id = t.id

    # --- 1. Concurrent workers claim each job exactly once (no double-claim) ---
    async def worker(wid: str, claimed: list):
        while True:
            async with Session() as db:
                jid = await job_queue.claim_job(db, wid)
            if jid is None:
                # Might just be contention on SQLite; retry briefly, then stop
                # only when nothing is left queued.
                async with Session() as db:
                    left = (
                        await db.execute(text("SELECT count(*) FROM jobs WHERE status='queued'"))
                    ).scalar()
                if left == 0:
                    return
                await asyncio.sleep(0.01)
                continue
            claimed.append(str(jid))

    buckets = [[] for _ in range(4)]
    await asyncio.gather(*[worker(f"w{i}", buckets[i]) for i in range(4)])
    all_claimed = [j for b in buckets for j in b]

    check("every queued job was claimed", len(all_claimed) == N, f"{len(all_claimed)}/{N}")
    check(
        "no job was claimed twice (atomic across 4 workers)",
        len(set(all_claimed)) == len(all_claimed),
        f"{len(all_claimed)} claims, {len(set(all_claimed))} unique",
    )
    async with Session() as db:
        running = (
            await db.execute(text("SELECT count(*) FROM jobs WHERE status='running'"))
        ).scalar()
        queued = (
            await db.execute(text("SELECT count(*) FROM jobs WHERE status='queued'"))
        ).scalar()
    check(
        "all jobs now running, none queued",
        running == N and queued == 0,
        f"running={running} queued={queued}",
    )

    # --- 2. reclaim_stale fails a running job with an old heartbeat ----------
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=9999)
    async with Session() as db:
        one = (await db.execute(select(Job.id).limit(1))).scalar()
        await db.execute(update(Job).where(Job.id == one).values(heartbeat_at=stale_time))
        await db.commit()
    counts = await job_queue.reclaim_stale(Session, lease_timeout=180)
    check("stale job reclaimed (>=1 failed)", counts["jobs"] >= 1, str(counts))
    async with Session() as db:
        st = (await db.get(Job, one)).status
    check("reclaimed job is now failed", st == "failed", st)

    # --- 3. A FRESH lease is NOT reclaimed (live worker not stolen) ----------
    async with Session() as db:
        fresh = (await db.execute(select(Job.id).where(Job.status == "running").limit(1))).scalar()
        await db.execute(
            update(Job).where(Job.id == fresh).values(heartbeat_at=datetime.now(timezone.utc))
        )
        await db.commit()
        before = (await db.get(Job, fresh)).status
    await job_queue.reclaim_stale(Session, lease_timeout=180)
    async with Session() as db:
        after = (await db.get(Job, fresh)).status
    check(
        "fresh-lease job left untouched by reclaim",
        before == "running" and after == "running",
        f"{before}->{after}",
    )

    # --- 4. Dataset-run sharding: children claimable + fan-in finalize -------
    async with Session() as db:
        ds = Dataset(
            id=uuid.uuid4(),
            name="q/ds",
            version="1.0",
            content_hash="x",
            task_ids=[str(task_id)],
            task_names=["q/task"],
        )
        db.add(ds)
        run = DatasetRun(
            id=uuid.uuid4(),
            dataset_id=ds.id,
            agent="oracle",
            n_trials=1,
            concurrency=2,
            status="running",
        )
        db.add(run)
        await db.flush()
        # Two queued CHILD jobs (dataset_run_id set) — the sharding units.
        c1 = Job(
            id=uuid.uuid4(),
            task_id=task_id,
            dataset_run_id=run.id,
            agent="oracle",
            n_trials=1,
            status="queued",
        )
        c2 = Job(
            id=uuid.uuid4(),
            task_id=task_id,
            dataset_run_id=run.id,
            agent="oracle",
            n_trials=1,
            status="queued",
        )
        db.add_all([c1, c2])
        await db.commit()
        run_id, c1_id, c2_id = run.id, c1.id, c2.id

    # Child jobs are claimable (that's what spreads a cohort across workers).
    async with Session() as db:
        got = await job_queue.claim_job(db, "wX")
    check("a dataset-run CHILD job is claimable", got in (c1_id, c2_id), str(got))

    # Run does NOT finalize while a child is still open.
    async with Session() as db:
        fin = await job_queue.finalize_run_if_done(db, run_id)
    check("run NOT finalized while a child is unfinished", fin is False)

    # Finish both children → the run finalizes (fan-in), exactly once.
    async with Session() as db:
        await db.execute(update(Job).where(Job.dataset_run_id == run_id).values(status="completed"))
        await db.commit()
    async with Session() as db:
        first = await job_queue.finalize_run_if_done(db, run_id)
    async with Session() as db:
        again = await job_queue.finalize_run_if_done(db, run_id)
    check("run finalizes once all children are terminal", first is True)
    check("finalize is idempotent (second caller gets False)", again is False)
    async with Session() as db:
        check("run status is completed", (await db.get(DatasetRun, run_id)).status == "completed")

    # A child left 'running' with a stale heartbeat is reclaimed AND its run
    # then finalized (so a crashed worker never wedges a cohort).
    async with Session() as db:
        run2 = DatasetRun(
            id=uuid.uuid4(),
            dataset_id=ds.id,
            agent="oracle",
            n_trials=1,
            concurrency=1,
            status="running",
        )
        db.add(run2)
        await db.flush()
        stuck = Job(
            id=uuid.uuid4(),
            task_id=task_id,
            dataset_run_id=run2.id,
            agent="oracle",
            n_trials=1,
            status="running",
            heartbeat_at=stale_time,
        )
        db.add(stuck)
        await db.commit()
        run2_id, stuck_id = run2.id, stuck.id
    await job_queue.reclaim_stale(Session, lease_timeout=180)
    async with Session() as db:
        check("stale child reclaimed to failed", (await db.get(Job, stuck_id)).status == "failed")
        check(
            "its run finalized after reclaim (no hang)",
            (await db.get(DatasetRun, run2_id)).status == "completed",
        )

    await engine.dispose()
    print(f"\n=========== {passed} passed, {failed} failed ===========")
    return 1 if failed else 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    if _tmp:
        import shutil

        shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(rc)
