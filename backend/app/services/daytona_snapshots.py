"""Content-addressed Daytona snapshots, so a task's image is built once per job.

Without this, every trial handed Daytona an `Image.from_dockerfile(...)` and paid
the build again: a 10-trial job built the same image ten times. A snapshot is
Daytona's reusable prebuilt form, so the work moves to the first trial and the
rest start from it.

The name is derived from everything that can change the resulting image — the
build context's contents *and* the resource shape, because Daytona bakes
resources into the snapshot and a sandbox created from one cannot override them.
Two tasks that differ only in requested memory must therefore not share a name,
or the second would silently run with the first's limits.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

from app.services.daytona_client import call_with_retry

log = logging.getLogger("tracetensor.daytona.snapshot")

NAME_PREFIX = "tracetensor"

#: Bumped when the *construction* of a snapshot changes (different base handling,
#: different bake steps) so old snapshots aren't reused under new semantics. The
#: build context's own hash covers task edits; this covers our edits.
_SCHEMA_VERSION = "1"

#: States meaning "someone else is building this right now — wait for them".
_PENDING_STATES = {"building", "pending", "pulling", "snapshotting"}
#: States meaning the snapshot is unusable and must be rebuilt from scratch.
_DEAD_STATES = {"error", "build_failed", "removing"}

# One lock per snapshot name. Trials in a job run on threads, and two of them
# reaching a cold snapshot together would otherwise both start a build of the
# same thing — the exact duplicated work this module exists to remove.
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock_for(name: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _locks[name] = lock
        return lock


def _hash_build_context(context_dir: Path) -> str:
    """A digest of every file that can affect the built image.

    Hashes paths as well as bytes: renaming a file changes what the Dockerfile
    copies even when the content is identical.
    """
    digest = hashlib.sha256()
    if not context_dir.is_dir():
        return "no-context"
    for path in sorted(p for p in context_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(context_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def snapshot_name(
    context_dir: Path,
    *,
    cpu: int,
    memory_gib: int,
    disk_gib: int,
) -> str:
    """A stable, collision-resistant snapshot name for this build context."""
    digest = hashlib.sha256()
    digest.update(_SCHEMA_VERSION.encode())
    digest.update(b"|")
    digest.update(_hash_build_context(context_dir).encode())
    digest.update(f"|cpu={cpu}|mem={memory_gib}|disk={disk_gib}".encode())
    return f"{NAME_PREFIX}-{digest.hexdigest()[:16]}"


def _state_of(snapshot) -> str:
    raw = getattr(snapshot, "state", None)
    text = str(raw or "").lower()
    # SnapshotState is an enum; str() gives "SnapshotState.ACTIVE".
    return text.rsplit(".", 1)[-1]


def _get(client, name: str):
    """The snapshot, or None when it does not exist."""
    try:
        return call_with_retry(
            lambda: client.snapshot.get(name), what=f"snapshot.get({name})"
        )
    except Exception as exc:
        if "not found" in str(exc).lower() or type(exc).__name__ == "DaytonaNotFoundError":
            return None
        raise


def _wait_until_settled(client, name: str, timeout: float):
    """Poll a building snapshot until it stops being in-progress."""
    deadline = time.time() + timeout
    delay = 2.0
    while time.time() < deadline:
        time.sleep(delay)
        delay = min(10.0, delay * 1.5)
        snapshot = _get(client, name)
        if snapshot is None or _state_of(snapshot) not in _PENDING_STATES:
            return snapshot
    raise TimeoutError(f"Snapshot {name!r} was still building after {timeout:.0f}s.")


def ensure_snapshot(client, name: str, image, resources, *, timeout: float) -> bool:
    """Make sure an ACTIVE snapshot called `name` exists. True if it is usable.

    Returns False rather than raising when Daytona simply cannot give us one
    (snapshots unavailable on the account, an unexpected terminal state) — the
    caller then falls back to building from the image, which is what it did
    before this module existed. A slow run beats a failed one.
    """
    with _lock_for(name):
        try:
            snapshot = _get(client, name)
            if snapshot is not None and _state_of(snapshot) in _PENDING_STATES:
                # Another process (not just another thread) may be building it.
                snapshot = _wait_until_settled(client, name, timeout)

            if snapshot is not None:
                state = _state_of(snapshot)
                if state == "active":
                    log.info("daytona_snapshot_hit name=%s", name)
                    return True
                if state in _DEAD_STATES:
                    log.warning("daytona_snapshot_dead name=%s state=%s — rebuilding", name, state)
                    _delete(client, name)
                    snapshot = None
                else:
                    log.warning("daytona_snapshot_unusable name=%s state=%s", name, state)
                    return False

            return _create(client, name, image, resources, timeout=timeout)
        except Exception as exc:
            log.warning(
                "daytona_snapshot_unavailable name=%s error=%s — falling back to image build",
                name,
                f"{type(exc).__name__}: {exc}"[:200],
            )
            return False


def _delete(client, name: str) -> None:
    try:
        snapshot = _get(client, name)
        if snapshot is not None:
            client.snapshot.delete(snapshot)
    except Exception:  # a snapshot we failed to delete is not worth failing the run
        log.debug("daytona_snapshot_delete_failed name=%s", name, exc_info=True)


def _create(client, name: str, image, resources, *, timeout: float) -> bool:
    from daytona import CreateSnapshotParams

    params = CreateSnapshotParams(name=name, image=image, resources=resources)
    log.info("daytona_snapshot_build name=%s", name)
    started = time.time()
    try:
        call_with_retry(
            lambda: client.snapshot.create(params, timeout=timeout),
            what=f"snapshot.create({name})",
            attempts=2,  # a build is expensive; one retry, not three
        )
    except Exception as exc:
        # A parallel builder elsewhere may have won the race between our get and
        # our create. That is a success for us, not a failure — adopt theirs.
        if type(exc).__name__ == "DaytonaConflictError" or "already exists" in str(exc).lower():
            log.info("daytona_snapshot_raced name=%s — adopting the existing build", name)
            settled = _wait_until_settled(client, name, timeout)
            return settled is not None and _state_of(settled) == "active"
        raise
    log.info("daytona_snapshot_built name=%s in=%.1fs", name, time.time() - started)
    return True
