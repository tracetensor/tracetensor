"""
Phase 2 API E2E test — full HTTP flow with a real LLM agent.

Exercises the actual REST endpoints end-to-end:
  1. POST  /ingest/task/upload           — register sort-csv from a ZIP
  2. POST  /examine/{task_id}            — start a job with claude agent
  3. GET   /examine/job/{job_id}         — poll until status=completed
  4. Assert reward, trajectory, and job shape

Requires:
  - Docker daemon running
  - ANTHROPIC_API_KEY (or OPENAI_API_KEY) in backend/.env
  - Backend API server running on http://localhost:8000
    (uvicorn app.main:app --port 8000)

Run:  cd backend && venv/bin/python tests/test_phase2_api_e2e.py
Or:   cd backend && venv/bin/python tests/test_phase2_api_e2e.py --provider anthropic --model claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

# Load .env so we know which providers are available for the assertion below.
from app.core.config import settings  # noqa: E402
from app.services import llm  # noqa: E402

PROJECT = HERE.parents[2]
EXAMPLES = PROJECT / "examples"

BASE_URL = "http://localhost:8000"
passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def http_get(path: str) -> dict:
    with urllib.request.urlopen(BASE_URL + path, timeout=30) as r:
        import json

        return json.loads(r.read().decode())


def http_post_json(path: str, body: dict) -> dict:
    import json

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def http_post_file(path: str, filepath: Path, field: str = "file") -> dict:
    """Multipart upload of a single file."""
    import json
    import uuid as _uuid

    boundary = "----tt-" + _uuid.uuid4().hex
    file_bytes = filepath.read_bytes()
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filepath.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def wait_for_health(url: str = BASE_URL + "/api/health", timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    return False


def pick_provider() -> tuple[str, str]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=list(llm.PROVIDERS), default=None)
    ap.add_argument("--model", default=None)
    args, _ = ap.parse_known_args()

    if args.provider:
        provider = args.provider
    else:
        avail = settings.available_providers()
        for p in ("anthropic", "openai", "openrouter"):
            if avail.get(p):
                provider = p
                break
        else:
            print("ERROR: no LLM provider key in backend/.env.")
            sys.exit(2)
    return provider, args.model or llm.default_model(provider)


def main() -> int:
    global passed, failed

    provider, model = pick_provider()
    print(f"\n== Phase 2 API E2E — provider={provider}, model={model} ==")

    # 0. Server must be up.
    if not wait_for_health():
        print(f"\nERROR: API server not reachable at {BASE_URL}.")
        print("Start it with:")
        print("  cd backend && venv/bin/uvicorn app.main:app --port 8000")
        return 2
    check("API server reachable", True)

    # 1. Providers endpoint reflects our env.
    providers_resp = http_get("/examine/providers")
    check(
        "/examine/providers returns agents",
        isinstance(providers_resp.get("agents"), list) and len(providers_resp["agents"]) > 0,
    )
    check(
        "/examine/providers has docker backend",
        any(b["id"] == "docker" for b in providers_resp.get("backends", [])),
    )
    agent_entry = next((a for a in providers_resp["agents"] if a["id"] == provider), None)
    check(f"provider '{provider}' listed in catalog", agent_entry is not None)
    check(
        f"provider '{provider}' is available (key set)",
        agent_entry is not None and agent_entry.get("available") is True,
    )

    # 2. Register the sort-csv task via ZIP upload.
    zip_path = EXAMPLES / "sort-csv.zip"
    if not zip_path.exists():
        print(f"\nERROR: {zip_path} not found. Zip up examples/sort-csv/ first.")
        return 2

    reg = http_post_file("/ingest/task/upload", zip_path, field="file")
    task_id = reg.get("id") or reg.get("task", {}).get("id")
    check("task registered with id", bool(task_id), f"id={task_id}")
    check(
        "task status=ready_for_examination",
        reg.get("status") == "ready_for_examination"
        or reg.get("task", {}).get("status") == "ready_for_examination",
        f"status={reg.get('status')}",
    )

    # 3. Start examination with the real LLM agent.
    job_resp = http_post_json(
        f"/examine/{task_id}",
        {
            "agent": provider,
            "model": model,
            "n_trials": 1,
            "backend": "docker",
        },
    )
    job_id = job_resp.get("id")
    check("job created with id", bool(job_id), f"job_id={job_id}")
    check(
        "job initial status is queued or running",
        job_resp.get("status") in ("queued", "running"),
        f"status={job_resp.get('status')}",
    )
    check("job records provider as agent", job_resp.get("agent") == provider)
    check("job records model", job_resp.get("model") == model)

    # 4. Poll until complete (docker build + LLM run + verify).
    print(f"\n  Polling /examine/job/{job_id} …")
    deadline = time.time() + 300  # 5 min budget for build + LLM + verify
    last_status = None
    job_final = None
    while time.time() < deadline:
        j = http_get(f"/examine/job/{job_id}")
        if j.get("status") != last_status:
            print(
                f"    [{time.time() - (deadline - 300):5.1f}s] status={j.get('status')} "
                f"trials_completed={j.get('trials_completed')} "
                f"trials_passed={j.get('trials_passed')}"
            )
            last_status = j.get("status")
        if j.get("status") in ("completed", "failed", "error"):
            job_final = j
            break
        time.sleep(1.5)

    check("job reached terminal status within budget", job_final is not None)
    if job_final is None:
        return 1

    print(
        f"\n  Final: status={job_final['status']} pass_rate={job_final.get('pass_rate')} "
        f"trials_passed={job_final['trials_passed']}/{job_final['n_trials']}"
    )

    check(
        "job completed (not failed)",
        job_final["status"] == "completed",
        f"status={job_final['status']} error={job_final.get('error')}",
    )
    check(
        "trials array populated",
        isinstance(job_final.get("trials"), list) and len(job_final["trials"]) == 1,
    )

    trial = (job_final.get("trials") or [{}])[0]
    check("trial has reward", trial.get("reward") is not None, f"reward={trial.get('reward')}")
    check(
        "trial reward in [0, 1]", trial.get("reward") is not None and 0.0 <= trial["reward"] <= 1.0
    )
    check(
        "trial has trajectory with steps",
        isinstance(trial.get("trajectory"), dict) and len(trial["trajectory"].get("steps", [])) > 0,
        f"steps={len(trial.get('trajectory', {}).get('steps', []))}",
    )
    check(
        "trial trajectory has agent phase",
        any(s.get("phase") == "agent" for s in trial.get("trajectory", {}).get("steps", [])),
    )
    check(
        "trial trajectory has verifier phase",
        any(s.get("phase") == "verifier" for s in trial.get("trajectory", {}).get("steps", [])),
    )
    check("trial verifier_log captured", bool(trial.get("verifier_log")))
    check(
        "job context includes instruction",
        isinstance(job_final.get("context"), dict)
        and job_final["context"].get("instruction") is not None,
    )

    # 5. Confirm the job now shows up in the jobs list.
    jobs_list = http_get("/examine/jobs")
    check("job appears in /examine/jobs list", any(j["id"] == job_id for j in jobs_list))

    # 6. Confirm task-scoped listing works.
    task_jobs = http_get(f"/examine/task/{task_id}/jobs")
    check("job appears in /examine/task/{id}/jobs list", any(j["id"] == job_id for j in task_jobs))

    reward_val = trial.get("reward")
    verdict_text = "SOLVED" if reward_val == 1.0 else f"reward={reward_val}"
    n_steps = len(trial.get("trajectory", {}).get("steps", []))
    print(
        f"\n  === LLM verdict: {verdict_text} "
        f"in {trial.get('duration_s', 0):.1f}s across {n_steps} step(s) ==="
    )

    print(f"\n{'=' * 60}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'=' * 60}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
