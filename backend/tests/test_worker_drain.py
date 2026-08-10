"""
Graceful-drain test — a job in flight when the API shuts down should FINISH,
not be abandoned. Boots a server with the embedded worker, enqueues an oracle
job, and SIGTERMs the server while the job is running; then reads the DB (which
outlives the process) and asserts the job completed cleanly.

Requires Docker (oracle runs a container). No API key.
Run:  cd backend && python tests/test_worker_drain.py
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(url):
    r = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp(prefix="tt_drain_"))
    db_path = tmp / "d.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "TASKS_ROOT": str(tmp / "tasks"),
        "WORKER_EMBEDDED": "true",
        "WORKER_DRAIN_TIMEOUT": "60",  # long enough for a quick oracle job to finish
    }
    (tmp / "tasks").mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/api/health", timeout=5)
                break
            except Exception:
                time.sleep(0.5)

        tid = post(f"{base}/v1/ingest/example")["id"]
        # Enqueue via urllib with a JSON body.
        body = json.dumps({"agent": "oracle", "n_trials": 1}).encode()
        r = urllib.request.Request(
            f"{base}/v1/examine/{tid}", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(r, timeout=30) as resp:
            job_id = json.loads(resp.read())["id"]
        print(f"  enqueued job {job_id}")

        # Wait until the worker has actually claimed it (status running), so the
        # SIGTERM lands mid-flight — that's the case we care about.
        running = False
        for _ in range(60):
            with urllib.request.urlopen(f"{base}/v1/examine/job/{job_id}", timeout=10) as resp:
                st = json.loads(resp.read())["status"]
            if st == "running":
                running = True
                break
            if st in ("completed", "failed"):
                break
            time.sleep(0.2)
        check("job was running before shutdown (drain is meaningful)", running, st)

        # Graceful shutdown mid-flight.
        print("  sending SIGTERM while job is in flight…")
        proc.send_signal(signal.SIGTERM)
        exited = proc.wait(timeout=90)
        check("server shut down (drain didn't hang)", exited is not None)

        # The DB outlives the process — read the job's final state directly.
        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id.replace("-", ""),)
        ).fetchone()
        if row is None:  # UUID may be stored with dashes depending on backend
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        trial = con.execute(
            "SELECT reward FROM trials WHERE job_id IN (SELECT id FROM jobs)"
        ).fetchone()
        con.close()

        check(
            "in-flight job COMPLETED on graceful drain (not abandoned/failed)",
            row is not None and row[0] == "completed",
            str(row),
        )
        check(
            "its trial was recorded with reward 1.0",
            trial is not None and trial[0] == 1.0,
            str(trial),
        )

    finally:
        if proc.poll() is None:
            proc.kill()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=========== {passed} passed, {failed} failed ===========")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
