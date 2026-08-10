"""
job_service — enqueueing work, with a real database and no HTTP.

These tests exist because the logic they cover decides whether a retried request
spends money twice. It used to live inline in two route handlers, so exercising
it meant booting a server and issuing real requests; now it's a function call
against a temp SQLite file.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import JobStatus, TaskStatus
from app.services import job_service
from app.services.agents import AgentConfigError


async def make_job(db, task, **kw):
    return await job_service.create_job(
        db,
        task_id=kw.pop("task_id", task.id),
        agent=kw.pop("agent", "oracle"),
        model=kw.pop("model", None),
        n_trials=kw.pop("n_trials", 1),
        concurrency=kw.pop("concurrency", 1),
        **kw,
    )


class TestCreateJob:
    async def test_queues_a_job(self, db, ready_task):
        created = await make_job(db, ready_task)
        assert created.existing is False
        assert created.job.status == JobStatus.QUEUED
        assert created.job.agent == "oracle"

    async def test_registers_an_event_bus_before_returning(self, db, ready_task):
        """An SSE client can connect the instant the POST returns. If the bus
        doesn't exist yet it misses the worker's opening events."""
        from app.services.event_bus import get_bus

        created = await make_job(db, ready_task)
        assert get_bus(str(created.job.id)) is not None

    async def test_unknown_task_is_not_found(self, db, ready_task):
        with pytest.raises(job_service.NotFound):
            await make_job(db, ready_task, task_id=uuid.uuid4())

    async def test_a_task_that_never_passed_validation_is_not_runnable(self, db, ready_task):
        ready_task.status = TaskStatus.REGISTERED.value
        await db.commit()
        with pytest.raises(job_service.NotRunnable):
            await make_job(db, ready_task)

    async def test_unknown_agent_is_rejected_before_any_row_is_written(self, db, ready_task):
        """Fail fast: a bad agent name should cost nothing, not one container
        per trial to discover."""
        from sqlalchemy import func, select

        from app.models.job import Job

        with pytest.raises(AgentConfigError):
            await make_job(db, ready_task, agent="not-a-real-agent")
        count = (await db.execute(select(func.count()).select_from(Job))).scalar_one()
        assert count == 0

    async def test_errors_share_one_base_class(self, db, ready_task):
        """Callers should be able to catch the family without enumerating it."""
        with pytest.raises(job_service.JobServiceError):
            await make_job(db, ready_task, task_id=uuid.uuid4())


class TestIdempotency:
    """A retried POST must never start a second run — a second run spends real
    money on a real model API."""

    async def test_same_key_returns_the_original_job(self, db, ready_task):
        first = await make_job(db, ready_task, idempotency_key="abc")
        second = await make_job(db, ready_task, idempotency_key="abc")

        assert second.job.id == first.job.id
        assert second.existing is True

    async def test_the_caller_can_tell_a_retry_from_a_new_run(self, db, ready_task):
        """`existing` is what lets a caller log/meter/bill correctly even though
        the HTTP response is identical either way."""
        assert (await make_job(db, ready_task, idempotency_key="k")).existing is False
        assert (await make_job(db, ready_task, idempotency_key="k")).existing is True

    async def test_different_keys_are_different_jobs(self, db, ready_task):
        a = await make_job(db, ready_task, idempotency_key="one")
        b = await make_job(db, ready_task, idempotency_key="two")
        assert a.job.id != b.job.id

    async def test_no_key_means_no_deduplication(self, db, ready_task):
        """Without a key we can't tell a retry from a genuine second run, so we
        must assume the latter."""
        a = await make_job(db, ready_task)
        b = await make_job(db, ready_task)
        assert a.job.id != b.job.id

    async def test_the_unique_index_decides_the_race_not_the_pre_check(self, db, ready_task):
        """The pre-check keeps the common case cheap; the DB constraint is what
        makes it correct. Simulated here by inserting a colliding row behind the
        service's back, exactly as a concurrent request would."""
        from app.models.job import Job

        db.add(
            Job(
                id=uuid.uuid4(),
                task_id=ready_task.id,
                agent="oracle",
                n_trials=1,
                concurrency=1,
                status=JobStatus.QUEUED.value,
                idempotency_key="racy",
            )
        )
        await db.commit()

        created = await make_job(db, ready_task, idempotency_key="racy")
        assert created.existing is True

        from sqlalchemy import func, select

        count = (await db.execute(select(func.count()).select_from(Job))).scalar_one()
        assert count == 1  # the loser did NOT enqueue a second paid run


class TestCreateDatasetRun:
    async def test_queues_one_child_job_per_ready_task(self, db, ready_task, tmp_path):
        from sqlalchemy import select

        from app.models.dataset import Dataset
        from app.models.job import Job

        ds = Dataset(
            id=uuid.uuid4(),
            name="suite",
            version="1.0",
            content_hash="h",
            task_ids=[str(ready_task.id)],
            task_names=[ready_task.name],
        )
        db.add(ds)
        await db.commit()

        created = await job_service.create_dataset_run(
            db, dataset_id=ds.id, agent="oracle", model=None, n_trials=2, concurrency=4
        )
        # The run is orchestration, not execution — it's RUNNING immediately and
        # is never claimed by a worker.
        assert created.run.status == JobStatus.RUNNING
        children = (
            (await db.execute(select(Job).where(Job.dataset_run_id == created.run.id)))
            .scalars()
            .all()
        )
        assert len(children) == 1
        assert children[0].status == JobStatus.QUEUED

    async def test_a_dataset_with_no_ready_tasks_is_not_runnable(self, db, ready_task):
        from app.models.dataset import Dataset

        ready_task.status = TaskStatus.REGISTERED.value
        ds = Dataset(
            id=uuid.uuid4(),
            name="suite",
            version="2.0",
            content_hash="h2",
            task_ids=[str(ready_task.id)],
            task_names=[ready_task.name],
        )
        db.add(ds)
        await db.commit()

        with pytest.raises(job_service.NotRunnable):
            await job_service.create_dataset_run(
                db, dataset_id=ds.id, agent="oracle", model=None, n_trials=1, concurrency=1
            )

    async def test_unknown_dataset_is_not_found(self, db):
        with pytest.raises(job_service.NotFound):
            await job_service.create_dataset_run(
                db, dataset_id=uuid.uuid4(), agent="oracle", model=None, n_trials=1, concurrency=1
            )


class TestListJobs:
    async def test_paginates_and_reports_the_total(self, db, ready_task):
        for _ in range(5):
            await make_job(db, ready_task)

        page, names, total = await job_service.list_jobs(db, limit=2, offset=0)
        assert len(page) == 2
        assert total == 5
        assert names[ready_task.id] == ready_task.name

    async def test_filters_by_task(self, db, ready_task):
        await make_job(db, ready_task)
        _page, _names, total = await job_service.list_jobs(
            db, task_id=uuid.uuid4(), limit=10, offset=0
        )
        assert total == 0
