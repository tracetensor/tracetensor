"""
Deterministic CI entry point — runs pytest under coverage and reports.

`pytest` is the runner; this wraps it with coverage collection, a combine step
(the e2e suites launch their own uvicorn subprocesses that record separately),
and a minimum-coverage gate. Both commands work:

  cd backend && venv/bin/python -m pytest          # what you run while working
  cd backend && venv/bin/python tests/run_ci.py    # what CI runs

The key-gated real-LLM suites (test_phase2_e2e, test_phase2_api_e2e) are NOT run
here — they need secrets and a live model, so they're a separate, manual step,
never a merge blocker.

Usage:
  cd backend && venv/bin/python tests/run_ci.py                 # everything
  cd backend && venv/bin/python tests/run_ci.py --offline-only  # skip the Docker tier
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PY = sys.executable

# The floor, not the target. It exists so coverage can't silently erode; raise it
# when a phase of work lifts the real number, never lower it to make CI pass.
MIN_COVERAGE = 70


def chromium_available() -> bool:
    """Whether the browser tier can run. Reported rather than assumed, because a
    silently-skipped UI suite is exactly the blind spot it exists to remove."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def main() -> int:
    offline_only = "--offline-only" in sys.argv

    subprocess.run([PY, "-m", "coverage", "erase"], cwd=str(BACKEND))

    args = [PY, "-m", "coverage", "run", "-p", "-m", "pytest", "-v"]
    if offline_only:
        print(">> --offline-only: skipping the Docker tier on purpose.")
        args += ["-m", "not docker"]
    elif not docker_available():
        # Loud, not silent: a skipped tier must never read as a pass. pytest
        # would skip these on its own; saying so here keeps the CI log honest.
        print(">> !! Docker is NOT available — the sandbox + e2e tier will SKIP.")
        print(">> Re-run with Docker to cover those paths.")

    if not offline_only and not chromium_available():
        print(">> !! Chromium is NOT installed — the dashboard smoke tests will SKIP.")
        print(">>    Install it with: python -m playwright install chromium")
        print(">>    Without them, a backend change can break the UI silently.")

    env = {
        # Lets any child process (the e2e suites' uvicorn servers) record its own
        # coverage via backend/sitecustomize.py.
        **os.environ,
        "COVERAGE_PROCESS_START": str(BACKEND / "pyproject.toml"),
    }
    result = subprocess.run(args, cwd=str(BACKEND), env=env)

    subprocess.run([PY, "-m", "coverage", "combine"], cwd=str(BACKEND))
    print(f"\n{'=' * 70}\n### COVERAGE\n{'=' * 70}", flush=True)
    subprocess.run([PY, "-m", "coverage", "report"], cwd=str(BACKEND))
    subprocess.run(
        [PY, "-m", "coverage", "xml", "-o", "coverage.xml", "--quiet"],
        cwd=str(BACKEND),
    )

    # Only gate on coverage when the full suite ran — an offline-only run
    # legitimately doesn't touch the Docker paths, and failing on that would
    # train people to ignore the gate.
    coverage_ok = True
    if not offline_only:
        gate = subprocess.run(
            [PY, "-m", "coverage", "report", f"--fail-under={MIN_COVERAGE}"],
            stdout=subprocess.DEVNULL,
            cwd=str(BACKEND),
        )
        coverage_ok = gate.returncode == 0
        if not coverage_ok:
            print(f"### COVERAGE below the {MIN_COVERAGE}% floor.")

    print(f"\n{'=' * 70}")
    ok = result.returncode == 0 and coverage_ok
    print("### RESULT: " + ("PASS" if ok else "FAIL"), flush=True)
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
