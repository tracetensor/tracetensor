"""
Router helpers — the shared plumbing, tested directly.

These are the functions that used to exist twice, drift apart, and get fixed in
only one copy. Now there's one of each, so there's one place to test them.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from fastapi import HTTPException, Request, UploadFile

from app.models.enums import JobStatus
from app.routers import _shared
from app.routers._views import _job_summary
from app.services.event_bus import create_bus


class FakeUpload:
    """Stands in for a Starlette UploadFile: read(n) returns at most n bytes."""

    def __init__(self, data: bytes):
        self.data = data

    async def read(self, n: int = -1) -> bytes:
        return self.data if n < 0 else self.data[:n]


class TestReadUpload:
    async def test_accepts_a_payload_within_the_cap(self):
        assert (
            await _shared.read_upload(cast(UploadFile, FakeUpload(b"x" * 100)), 1000) == b"x" * 100
        )

    async def test_rejects_an_oversized_payload_with_413(self):
        with pytest.raises(HTTPException) as exc:
            await _shared.read_upload(cast(UploadFile, FakeUpload(b"x" * 2000)), 1000)
        assert exc.value.status_code == 413

    async def test_reads_one_byte_past_the_cap_to_detect_the_overflow(self):
        """It must not trust a declared Content-Length — that header is
        client-supplied and lying about it is the attack."""
        upload = cast(UploadFile, FakeUpload(b"x" * 1001))
        with pytest.raises(HTTPException):
            await _shared.read_upload(upload, 1000)

    async def test_exactly_at_the_cap_is_allowed(self):
        got = await _shared.read_upload(cast(UploadFile, FakeUpload(b"x" * 1000)), 1000)
        assert len(got) == 1000


class TestValidateTrialParams:
    def test_returns_the_effective_concurrency(self):
        assert _shared.validate_trial_params(10, 4, ceiling=10) == 4

    def test_a_single_task_never_exceeds_its_trial_count(self):
        """More workers than trials is waste, not speed."""
        assert _shared.validate_trial_params(2, 16, ceiling=2) == 2

    def test_a_dataset_run_uses_the_global_cap_instead(self):
        """Trials from different tasks fill the slots, so one task's count
        shouldn't bound the run. The two endpoints differ on purpose."""
        assert _shared.validate_trial_params(1, 16, ceiling=_shared.MAX_CONCURRENCY) == 16

    def test_omitted_concurrency_falls_back_to_the_default(self):
        assert _shared.validate_trial_params(10, None, ceiling=10) == _shared.DEFAULT_CONCURRENCY

    @pytest.mark.parametrize("n_trials", [0, -1, 51, 1000])
    def test_rejects_an_out_of_range_trial_count(self, n_trials):
        with pytest.raises(HTTPException) as exc:
            _shared.validate_trial_params(n_trials, 1, ceiling=1)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("concurrency", [0, -1, 17])
    def test_rejects_an_out_of_range_concurrency(self, concurrency):
        with pytest.raises(HTTPException) as exc:
            _shared.validate_trial_params(1, concurrency, ceiling=16)
        assert exc.value.status_code == 400


class TestSSE:
    def test_frames_an_event_in_the_wire_format(self):
        assert _shared.sse({"type": "ping"}) == 'data: {"type": "ping"}\n\n'

    def test_the_payload_round_trips_as_json(self):
        evt = {"type": "step", "stdout": "line one\nline two", "exit_code": 0}
        body = _shared.sse(evt)
        assert json.loads(body[len("data: ") :].strip()) == evt

    def test_deadline_is_twice_the_lease_timeout(self, settings_override):
        """Past the lease the worker is presumed dead and its lease is being
        reclaimed, so no further event can ever arrive."""
        import time

        with settings_override(WORKER_LEASE_TIMEOUT=10):
            assert _shared.stream_deadline() - time.monotonic() == pytest.approx(20, abs=1)


class FakeRequest:
    def __init__(self, disconnect_after: int = 999):
        self.checks = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.disconnect_after


class TestTailBus:
    async def test_replays_history_then_stops_at_the_terminal_event(self):
        bus = create_bus("j1")
        bus.emit({"type": "trial_started"})
        bus.emit({"type": "job_done", "status": "completed"})

        chunks = [
            c async for c in _shared.tail_bus(bus, cast(Request, FakeRequest()), poll_interval=0)
        ]
        assert len(chunks) == 2
        assert "trial_started" in chunks[0]

    async def test_stops_when_the_client_disconnects(self):
        bus = create_bus("j2")  # never marked done
        bus.emit({"type": "phase"})
        chunks = [
            c
            async for c in _shared.tail_bus(
                bus, cast(Request, FakeRequest(disconnect_after=1)), poll_interval=0
            )
        ]
        assert len(chunks) <= 1

    async def test_emits_an_error_event_when_the_deadline_passes(self, settings_override):
        """A crashed worker must not leave the browser on a spinner forever —
        the client learns the stream died instead of just going quiet."""
        bus = create_bus("j3")  # live, never finishes
        with settings_override(WORKER_LEASE_TIMEOUT=-1):  # already past the deadline
            chunks = [
                c
                async for c in _shared.tail_bus(bus, cast(Request, FakeRequest()), poll_interval=0)
            ]
        assert any("timed out" in c for c in chunks)


class TestJobSummary:
    def _job(self, **kw):
        from app.models.job import Job

        now = datetime.now(timezone.utc)
        defaults = dict(
            id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            dataset_run_id=None,
            agent="oracle",
            model=None,
            status=JobStatus.COMPLETED.value,
            n_trials=2,
            trials_passed=1,
            pass_rate=0.5,
            created_at=now,
            finished_at=now + timedelta(seconds=30),
        )
        job = Job(**{**defaults, **kw})
        job.trials = kw.pop("trials", [])
        return job

    def test_prefers_wall_clock_once_the_job_has_finished(self):
        """Trials run concurrently, so summing their durations overstates how
        long the job actually took."""
        row = _job_summary(self._job(), "my-task")
        assert row.duration_s == pytest.approx(30.0)

    def test_carries_the_task_name_through(self):
        assert _job_summary(self._job(), "my-task").task_name == "my-task"

    def test_an_unfinished_job_has_no_wall_clock(self):
        row = _job_summary(self._job(finished_at=None), None)
        assert row.duration_s is None


class TestEventBusBackends:
    """Two backends, one interface. The database one is what makes live progress
    work when the worker is on a different machine from the web tier — the case
    that silently produced an empty stream before."""

    async def test_memory_backend_replays_then_reports_done(self):
        from app.services import event_bus

        event_bus.configure_backend(event_bus.MemoryBackend())
        bus = create_bus("m1")
        bus.emit({"type": "phase", "phase": "agent"})
        bus.emit({"type": "job_done", "status": "completed"})

        events, cursor = await bus.read_from(0)
        assert [e["type"] for e in events] == ["phase", "job_done"]
        assert await bus.is_done()

        # A second read from the new cursor returns nothing — no duplicates.
        again, _ = await bus.read_from(cursor)
        assert again == []

    async def test_database_backend_round_trips_through_the_table(self, db):
        """The whole point: an event written by one process is readable by
        another. Same session factory here, but the path is the table."""
        from app.services import event_bus

        class _Factory:
            def __call__(self):
                class _Ctx:
                    async def __aenter__(_s):
                        return db

                    async def __aexit__(_s, *a):
                        return False

                return _Ctx()

        backend = event_bus.DatabaseBackend(_Factory())
        event_bus.configure_backend(backend)

        bus = create_bus("d1")
        bus.emit({"type": "phase", "phase": "setup"})
        bus.emit({"type": "job_done", "status": "completed"})
        await backend._flush_once()

        events, cursor = await bus.read_from(0)
        assert [e["type"] for e in events] == ["phase", "job_done"]
        assert cursor > 0, "the cursor must be a row id, not an offset"
        assert await bus.is_done()

        again, _ = await bus.read_from(cursor)
        assert again == [], "re-reading from the cursor must not repeat events"

        event_bus.configure_backend(event_bus.MemoryBackend())

    async def test_channels_do_not_bleed_into_each_other(self, db):
        from app.services import event_bus

        class _Factory:
            def __call__(self):
                class _Ctx:
                    async def __aenter__(_s):
                        return db

                    async def __aexit__(_s, *a):
                        return False

                return _Ctx()

        backend = event_bus.DatabaseBackend(_Factory())
        event_bus.configure_backend(backend)

        create_bus("a").emit({"type": "phase", "phase": "a-only"})
        create_bus("b").emit({"type": "phase", "phase": "b-only"})
        await backend._flush_once()

        a_events, _ = await create_bus("a").read_from(0)
        assert [e["phase"] for e in a_events] == ["a-only"]

        event_bus.configure_backend(event_bus.MemoryBackend())


class TestShouldTail:
    """Whether a stream follows live progress or sends a terminal snapshot.

    This exists because getting it wrong is invisible. Deciding purely from
    "does the event log have rows" broke live progress the moment events moved
    into the database: a job queued milliseconds ago has none, so the stream
    sent a snapshot of nothing and closed, and the dashboard showed a run that
    never appeared to start. No error anywhere — the browser smoke test caught
    it by timing out.
    """

    def test_a_just_queued_job_tails_even_with_no_events_yet(self):
        assert _shared.should_tail(JobStatus.QUEUED, has_events=False) is True

    def test_a_running_job_tails(self):
        assert _shared.should_tail(JobStatus.RUNNING, has_events=False) is True

    def test_a_finished_job_with_no_events_gets_a_snapshot(self):
        assert _shared.should_tail(JobStatus.COMPLETED, has_events=False) is False
        assert _shared.should_tail(JobStatus.FAILED, has_events=False) is False

    def test_a_finished_job_with_events_still_replays_them(self):
        """Reconnecting to a run that just completed should show the history,
        not jump straight to the summary."""
        assert _shared.should_tail(JobStatus.COMPLETED, has_events=True) is True
