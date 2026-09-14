"""Snapshot reuse, retry policy and client sharing for the Daytona backend.

All offline: every Daytona call is faked. What is asserted here is the decision
logic — when we retry, when we refuse to, when a snapshot is reused, and when we
fall back to building from an image rather than failing the run.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app.services import daytona_client, daytona_snapshots
from app.services.daytona_client import call_with_retry, get_client, is_transient, reset_clients
from app.services.daytona_snapshots import ensure_snapshot, snapshot_name


def _context(tmp: str, dockerfile: str = "FROM python:3.11-slim\n", extra: dict | None = None):
    ctx = Path(tmp) / "environment"
    ctx.mkdir(parents=True)
    (ctx / "Dockerfile").write_text(dockerfile)
    for name, body in (extra or {}).items():
        path = ctx / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return ctx


class _Boom(Exception):
    pass


class SnapshotNameTests(unittest.TestCase):
    def test_same_context_and_resources_give_the_same_name(self) -> None:
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            args = dict(cpu=1, memory_gib=2, disk_gib=3)
            self.assertEqual(
                snapshot_name(_context(a), **args),
                snapshot_name(_context(b), **args),
            )

    def test_changing_the_dockerfile_changes_the_name(self) -> None:
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            args = dict(cpu=1, memory_gib=2, disk_gib=3)
            self.assertNotEqual(
                snapshot_name(_context(a, "FROM python:3.11-slim\n"), **args),
                snapshot_name(_context(b, "FROM python:3.12-slim\n"), **args),
            )

    def test_changing_a_copied_file_changes_the_name(self) -> None:
        """The Dockerfile COPYs the context, so a data file edit changes the image."""
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            args = dict(cpu=1, memory_gib=2, disk_gib=3)
            self.assertNotEqual(
                snapshot_name(_context(a, extra={"info/src.md": "one"}), **args),
                snapshot_name(_context(b, extra={"info/src.md": "two"}), **args),
            )

    def test_renaming_a_file_changes_the_name(self) -> None:
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            args = dict(cpu=1, memory_gib=2, disk_gib=3)
            self.assertNotEqual(
                snapshot_name(_context(a, extra={"info/a.md": "same"}), **args),
                snapshot_name(_context(b, extra={"info/b.md": "same"}), **args),
            )

    def test_resources_are_part_of_the_name(self) -> None:
        """Daytona bakes resources into a snapshot and a sandbox cannot override
        them, so a task asking for more memory must not reuse the smaller one."""
        with TemporaryDirectory() as tmp:
            ctx = _context(tmp)
            small = snapshot_name(ctx, cpu=1, memory_gib=2, disk_gib=3)
            large = snapshot_name(ctx, cpu=1, memory_gib=8, disk_gib=3)
            self.assertNotEqual(small, large)

    def test_name_is_prefixed_and_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            name = snapshot_name(_context(tmp), cpu=1, memory_gib=2, disk_gib=3)
        self.assertTrue(name.startswith("tracetensor-"), name)
        self.assertLessEqual(len(name), 32)


class RetryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.slept: list[float] = []

    def _retry(self, fn, **kw):
        return call_with_retry(fn, what="test", sleep=self.slept.append, **kw)

    def test_transient_failures_are_retried_then_succeed(self) -> None:
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise _Boom("connection reset")
            return "ok"

        with patch.object(daytona_client, "transient_error_types", return_value=(_Boom,)):
            self.assertEqual(self._retry(flaky), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.slept, [1.0, 2.0], "backoff should grow")

    def test_permanent_failures_are_not_retried(self) -> None:
        """A bad key fails identically next time; retrying only delays the error."""
        calls = []

        def bad_key():
            calls.append(1)
            raise _Boom("unauthorized")

        with patch.object(daytona_client, "transient_error_types", return_value=()):
            with self.assertRaises(_Boom):
                self._retry(bad_key)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.slept, [])

    def test_gives_up_after_the_attempt_budget(self) -> None:
        calls = []

        def always():
            calls.append(1)
            raise _Boom("503")

        with patch.object(daytona_client, "transient_error_types", return_value=(_Boom,)):
            with self.assertRaises(_Boom):
                self._retry(always, attempts=3)
        self.assertEqual(len(calls), 3)

    def test_backoff_is_capped(self) -> None:
        with patch.object(daytona_client, "transient_error_types", return_value=(_Boom,)):
            with self.assertRaises(_Boom):
                self._retry(
                    MagicMock(side_effect=_Boom("x")), attempts=6, base_delay=1.0, max_delay=4.0
                )
        self.assertEqual(max(self.slept), 4.0)

    def test_missing_sdk_means_retry_nothing(self) -> None:
        """A failed import must not turn into 'retry every error', which would
        make a bad API key take three round trips to report."""
        with patch.dict("sys.modules", {"daytona": None}):
            self.assertEqual(daytona_client.transient_error_types(), ())
        self.assertFalse(is_transient(_Boom("x")))


class ClientSharingTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_clients()
        self.addCleanup(reset_clients)

    def test_same_credentials_reuse_one_client(self) -> None:
        made = []

        class FakeDaytona:
            def __init__(self, cfg):
                made.append(cfg)

        with patch.dict(
            "sys.modules",
            {"daytona": MagicMock(Daytona=FakeDaytona, DaytonaConfig=lambda **kw: kw)},
        ):
            a = get_client("key-1")
            b = get_client("key-1")
        self.assertIs(a, b)
        self.assertEqual(len(made), 1, "a second client was built for the same credentials")

    def test_different_credentials_get_different_clients(self) -> None:
        class FakeDaytona:
            def __init__(self, cfg):
                self.cfg = cfg

        with patch.dict(
            "sys.modules",
            {"daytona": MagicMock(Daytona=FakeDaytona, DaytonaConfig=lambda **kw: kw)},
        ):
            self.assertIsNot(get_client("key-1"), get_client("key-2"))
            self.assertIsNot(get_client("key-1"), get_client("key-1", "https://eu.example"))


def _snapshot(state: str):
    snap = MagicMock()
    snap.state = state
    return snap


class EnsureSnapshotTests(unittest.TestCase):
    def _client(self):
        client = MagicMock()
        client.snapshot = MagicMock()
        return client

    def test_active_snapshot_is_reused_without_building(self) -> None:
        client = self._client()
        client.snapshot.get.return_value = _snapshot("SnapshotState.ACTIVE")
        self.assertTrue(ensure_snapshot(client, "n1", "img", "res", timeout=60))
        client.snapshot.create.assert_not_called()

    def test_missing_snapshot_is_built(self) -> None:
        client = self._client()
        client.snapshot.get.side_effect = Exception("Snapshot not found")
        with patch.dict("sys.modules", {"daytona": MagicMock()}):
            self.assertTrue(ensure_snapshot(client, "n2", "img", "res", timeout=60))
        client.snapshot.create.assert_called_once()

    def test_failed_snapshot_is_deleted_and_rebuilt(self) -> None:
        client = self._client()
        client.snapshot.get.return_value = _snapshot("SnapshotState.BUILD_FAILED")
        with patch.dict("sys.modules", {"daytona": MagicMock()}):
            self.assertTrue(ensure_snapshot(client, "n3", "img", "res", timeout=60))
        client.snapshot.delete.assert_called_once()
        client.snapshot.create.assert_called_once()

    def test_unavailable_snapshots_fall_back_rather_than_fail_the_run(self) -> None:
        """Not every account can create snapshots. A slow run beats a dead one."""
        client = self._client()
        client.snapshot.get.side_effect = Exception("snapshots are not enabled")
        with patch.dict("sys.modules", {"daytona": MagicMock()}):
            client.snapshot.create.side_effect = Exception("forbidden")
            self.assertFalse(ensure_snapshot(client, "n4", "img", "res", timeout=60))

    def test_concurrent_builders_are_serialised(self) -> None:
        """Two trial threads hitting a cold snapshot must not build at once.

        Asserted by measuring overlap inside the build, not by call counts: two
        sequential builds and two simultaneous ones both call create twice, and
        only the simultaneous pair is the bug.
        """
        import threading
        import time as _time

        client = self._client()
        client.snapshot.get.side_effect = Exception("not found")

        guard = threading.Lock()
        inside = 0
        peak = 0

        def slow_create(*a, **kw):
            nonlocal inside, peak
            with guard:
                inside += 1
                peak = max(peak, inside)
            _time.sleep(0.05)
            with guard:
                inside -= 1
            return _snapshot("SnapshotState.ACTIVE")

        client.snapshot.create.side_effect = slow_create
        results: list[bool] = []

        def worker():
            results.append(ensure_snapshot(client, "shared", "img", "res", timeout=60))

        # Patched once around the whole thread lifecycle, not inside each worker:
        # patch.dict restores on exit, so a per-thread patch lets the first
        # finisher hand the still-running thread the real SDK mid-flight.
        with patch.dict("sys.modules", {"daytona": MagicMock()}):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(results, [True, True])
        self.assertEqual(peak, 1, "two builds of the same snapshot overlapped")

    def test_name_lock_is_per_name(self) -> None:
        a = daytona_snapshots._lock_for("alpha")
        b = daytona_snapshots._lock_for("beta")
        self.assertIsNot(a, b)
        self.assertIs(a, daytona_snapshots._lock_for("alpha"))


if __name__ == "__main__":
    unittest.main()
