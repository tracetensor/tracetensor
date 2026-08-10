"""
True end-to-end test: dataset → run → aggregate scoreboard, over real HTTP.

This is the deterministic, CI-runnable e2e the suite was missing. Unlike the
key-gated phase-2 e2e tests, it uses the ORACLE agent (runs each task's own
solve.sh) so it needs Docker but NO API key and produces the same result every
time — exactly what CI can gate a merge on.

It is fully self-contained: it boots its own uvicorn server against a throwaway
SQLite DB + temp tasks dir, drives the real REST endpoints with urllib, and
tears the server down. Nothing external needs to be running.

Exercises the whole differentiator path that had zero e2e coverage before:
  1. POST /datasets/example                  — create a multi-task dataset
  2. POST /datasets/{id}/examine (oracle)    — one run across every task
  3. (retry with same Idempotency-Key)       — must return the SAME run
  4. GET  /datasets/runs/{id}                — poll to completion, assert scores
  5. GET  /datasets/{id}/leaderboard         — aggregate scoreboard reflects it

Requires: Docker daemon + python:3.11-slim. No API key.
Run:  cd backend && venv/bin/python tests/test_e2e_dataset_flow.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _req(method: str, url: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp(prefix="tt_e2e_"))
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp / 'e2e.db'}",
        "TASKS_ROOT": str(tmp / "tasks"),
        # Explicit: conftest sets WORKER_EMBEDDED=false for the pytest process
        # and this subprocess inherits its environment. Without it the server
        # has no worker, the job never executes, and the test waits out its
        # timeout instead of failing with a reason.
        "WORKER_EMBEDDED": "true",
    }
    (tmp / "tasks").mkdir(parents=True, exist_ok=True)

    print(f"== booting server on :{port} (temp DB, oracle agent, no keys) ==")
    # When the CI runner measures coverage (COVERAGE_PROCESS_START set), wrap the
    # server in `coverage run -p` so the ROUTERS it exercises are counted — they
    # run only in this subprocess, so without this they'd read as 0% covered even
    # though the e2e drives them fully. uvicorn shuts down gracefully on SIGTERM,
    # letting coverage flush its data file on the way out.
    launcher = [sys.executable, "-m"]
    if env.get("COVERAGE_PROCESS_START"):
        launcher += ["coverage", "run", "-p", "-m"]
    proc = subprocess.Popen(
        [*launcher, "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for health (server + DB init).
        for _ in range(60):
            try:
                st, _ = _req("GET", f"{base}/api/health")
                if st == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("  FAIL  server never came up")
            return 1

        print("\n== 1. create the example dataset ==")
        st, ds = _req("POST", f"{base}/datasets/example")
        check("dataset created", st == 200, f"status {st}")
        dataset_id = ds.get("id")
        task_count = ds.get("task_count", 0)
        check(
            "dataset has >= 2 tasks (multi-task run is the point)",
            task_count >= 2,
            f"task_count={task_count}",
        )

        print("\n== 2. run the ORACLE agent across the whole dataset (zero API cost) ==")
        idem = "e2e-oracle-run-fixed-key"
        st, run = _req(
            "POST",
            f"{base}/datasets/{dataset_id}/examine",
            {"agent": "oracle", "n_trials": 1, "concurrency": 2},
            headers={"Idempotency-Key": idem},
        )
        check("run accepted (oracle needs no key)", st == 200, f"status {st}: {str(run)[:160]}")
        run_id = run.get("id")

        print("\n== 3. retry with the same Idempotency-Key -> same run (no duplicate) ==")
        st2, run2 = _req(
            "POST",
            f"{base}/datasets/{dataset_id}/examine",
            {"agent": "oracle", "n_trials": 1, "concurrency": 2},
            headers={"Idempotency-Key": idem},
        )
        check(
            "idempotent retry returns the SAME run",
            run2.get("id") == run_id,
            f"{run2.get('id')} vs {run_id}",
        )

        print("\n== 4. poll to completion, assert every task scored 1.0 ==")
        final = None
        for _ in range(180):
            st, final = _req("GET", f"{base}/datasets/runs/{run_id}")
            if final.get("status") in ("completed", "failed"):
                break
            time.sleep(1)
        check(
            "run completed",
            final and final.get("status") == "completed",
            final.get("status") if final else "no response",
        )
        check(
            "all tasks completed",
            final.get("tasks_completed") == task_count,
            f"{final.get('tasks_completed')}/{task_count}",
        )
        check(
            "overall pass rate is 1.0 (oracle solves every task)",
            final.get("overall_pass_rate") == 1.0,
            str(final.get("overall_pass_rate")),
        )
        scores = final.get("scores") or []
        check("a score row per task", len(scores) == task_count, f"{len(scores)} rows")
        check(
            "every task score passed",
            all(s.get("pass_rate") == 1.0 for s in scores),
            str([s.get("pass_rate") for s in scores]),
        )

        print("\n== 5. aggregate scoreboard (leaderboard) reflects the run ==")
        st, lb = _req("GET", f"{base}/datasets/{dataset_id}/leaderboard")
        check("leaderboard returned", st == 200, f"status {st}")
        rows = lb.get("rows") or []
        check("exactly one agent row (oracle)", len(rows) == 1, f"{len(rows)} rows")
        if rows:
            row = rows[0]
            check("row is the oracle agent", row.get("agent") == "oracle", str(row.get("agent")))
            check(
                "row overall pass rate 1.0",
                row.get("overall_pass_rate") == 1.0,
                str(row.get("overall_pass_rate")),
            )
            cells = row.get("cells") or []
            check("a scoreboard cell per task", len(cells) == task_count, f"{len(cells)} cells")
            check(
                "every cell passed",
                all(c.get("pass_rate") == 1.0 for c in cells),
                str([c.get("pass_rate") for c in cells]),
            )
        check("leaderboard lists the dataset's tasks", len(lb.get("tasks") or []) == task_count)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n=========== {passed} passed, {failed} failed ===========")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
