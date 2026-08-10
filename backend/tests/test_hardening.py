"""
Hardening suite — auth gate, rate limit, readiness, metrics. Offline: boots its
own server against a temp SQLite DB with a token + a tiny rate limit configured,
no Docker and no API key. (Auth/rate-limit run as dependencies BEFORE any job
executes, so we never need a real agent to exercise them.)

Run:  cd backend && python tests/test_hardening.py
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
import uuid
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))

TOKEN = "secret-token-123"
RATE = 3

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
    p = s.getsockname()[1]
    s.close()
    return p


def req(method: str, url: str, headers=None):
    r = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def check_secrets() -> None:
    """File-based secrets (Docker-secrets convention) — a *_FILE path wins over
    the plain env var and is stripped. Pure function check, no server needed."""
    print("== file-based secrets (_secret helper) ==")
    from app.core.config import _secret

    tmp = Path(tempfile.mkdtemp(prefix="tt_secret_"))
    try:
        (tmp / "k").write_text("sk-from-file\n")
        os.environ["TT_SECRET_TEST"] = "sk-from-env"
        os.environ["TT_SECRET_TEST_FILE"] = str(tmp / "k")
        check("*_FILE wins over env + strips newline", _secret("TT_SECRET_TEST") == "sk-from-file")
        del os.environ["TT_SECRET_TEST_FILE"]
        check("env used when no *_FILE", _secret("TT_SECRET_TEST") == "sk-from-env")
        del os.environ["TT_SECRET_TEST"]
        check("None when neither set", _secret("TT_SECRET_TEST") is None)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("TT_SECRET_TEST", None)
        os.environ.pop("TT_SECRET_TEST_FILE", None)


def main() -> int:
    check_secrets()
    print()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp(prefix="tt_hard_"))
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp / 'h.db'}",
        "TASKS_ROOT": str(tmp / "tasks"),
        "API_TOKEN": TOKEN,
        "RATE_LIMIT_PER_MINUTE": str(RATE),
        "WORKER_EMBEDDED": "false",  # no worker needed; we never run a job
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
                st, _ = req("GET", f"{base}/api/health")
                if st == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("  FAIL  server never came up")
            return 1

        bearer = {"Authorization": f"Bearer {TOKEN}"}

        print("== ops endpoints are open (no token needed) ==")
        st, _ = req("GET", f"{base}/api/health")
        check("health open", st == 200, str(st))
        st, body = req("GET", f"{base}/api/ready")
        check("ready open + DB ok", st == 200 and json.loads(body).get("db") == "ok", body[:100])
        st, body = req("GET", f"{base}/api/metrics")
        check(
            "metrics open + prometheus text",
            st == 200 and "tracetensor_queue_depth" in body,
            str(st),
        )

        print("\n== auth gate (API_TOKEN set) ==")
        st, _ = req("GET", f"{base}/ingest/tasks")
        check("read WITHOUT token -> 401", st == 401, str(st))
        st, _ = req("GET", f"{base}/ingest/tasks", bearer)
        check("read WITH bearer token -> 200", st == 200, str(st))
        st, _ = req("GET", f"{base}/ingest/tasks", {"Authorization": "Bearer wrong"})
        check("read WITH wrong token -> 401", st == 401, str(st))
        st, _ = req("GET", f"{base}/ingest/tasks", {"X-API-Key": TOKEN})
        check("read WITH X-API-Key -> 200", st == 200, str(st))
        st, _ = req("GET", f"{base}/ingest/tasks?token={TOKEN}")
        check("read WITH ?token= query (SSE path) -> 200", st == 200, str(st))
        st, _ = req("POST", f"{base}/examine/{uuid.uuid4()}")
        check("POST job WITHOUT token -> 401 (money-spending gated)", st == 401, str(st))

        print("\n== API versioning ==")
        st, _ = req("GET", f"{base}/v1/ingest/tasks", bearer)
        check("canonical /v1/ingest/tasks -> 200", st == 200, str(st))
        st, _ = req("GET", f"{base}/ingest/tasks", bearer)
        check("deprecated unversioned alias still works -> 200", st == 200, str(st))
        st, _ = req("GET", f"{base}/v1/ingest/tasks")
        check("/v1 route is also auth-gated -> 401 without token", st == 401, str(st))

        print("\n== rate limit on job creation ==")
        # Authed POSTs to a nonexistent task: each passes auth + the rate-limit
        # dependency (incrementing) then 404s in the handler — until the limit.
        codes = []
        for _ in range(RATE + 2):
            st, _ = req("POST", f"{base}/examine/{uuid.uuid4()}", bearer)
            codes.append(st)
        allowed = sum(1 for c in codes if c != 429)
        got_429 = any(c == 429 for c in codes)
        check(f"first {RATE} allowed (not rate-limited)", allowed == RATE, f"codes={codes}")
        check("further requests -> 429 Too Many Requests", got_429, f"codes={codes}")

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
