"""
Runs the hand-rolled script suites under pytest, one test per suite.

Those suites (test_core.py, test_schema.py, …) predate pytest here. They're
plain scripts that print PASS/FAIL and exit nonzero, and several of them
deliberately run without pytest at all — test_core.py installs a pydantic shim
and exercises the parser with no third-party packages installed, which is a
genuinely useful property to keep.

Rather than rewrite ~1,500 lines of working, load-bearing assertions (and risk
losing coverage in the translation), this adapts them: `pytest` becomes the one
command a contributor needs to know, and each suite shows up as a named test
whose output is the suite's own PASS/FAIL lines. New tests are written directly
in pytest; these are grandfathered, not blessed.

The Docker-backed suites are marked and skipped when there's no daemon, so
`pytest` is green on a laptop without Docker.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
TESTS = HERE.parent
BACKEND = TESTS.parent

OFFLINE_SUITES = [
    "test_core.py",
    "test_server_e2e.py",
    "test_guardrails.py",
    "test_job_queue.py",
    "test_hardening.py",
    "test_cli.py",
    "test_features.py",
    "test_vault.py",
    "test_agent_silent_failure.py",
    "test_correctness.py",
    "test_schema.py",
    "test_architecture.py",
    "test_run_warnings.py",
    "test_platform_infra_retry.py",
]

DOCKER_SUITES = [
    "test_phase1_fidelity.py",
    "test_oracle_agent.py",
    "test_guardrail_integration.py",
    "test_e2e_dataset_flow.py",
    "test_worker_drain.py",
]


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def run_suite(name: str) -> None:
    """Run one script suite; fail the test with its output if it exits nonzero.

    Under CI the suite is launched via `coverage run -p` so its lines are
    counted. Without that these are opaque subprocesses and everything they
    exercise — the CLI, the vault rollups, the parsers — reads as untested.
    """
    import os

    cmd = [sys.executable]
    if os.environ.get("COVERAGE_PROCESS_START"):
        cmd += ["-m", "coverage", "run", "-p"]
    cmd.append(str(TESTS / name))

    proc = subprocess.run(
        cmd,
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        failures = [ln for ln in proc.stdout.splitlines() if "FAIL" in ln]
        detail = "\n".join(failures) or proc.stdout[-3000:]
        pytest.fail(f"{name} exited {proc.returncode}\n{detail}\n{proc.stderr[-2000:]}")


@pytest.mark.parametrize("suite", OFFLINE_SUITES)
def test_offline_suite(suite):
    run_suite(suite)


@pytest.mark.docker
@pytest.mark.parametrize("suite", DOCKER_SUITES)
def test_docker_suite(suite):
    if not docker_available():
        pytest.skip("Docker is not available — the sandbox tier can't run here.")
    run_suite(suite)


# Suites that are intentionally neither run here nor collected by pytest.
KEY_GATED_SUITES = ["test_phase2_e2e.py", "test_phase2_api_e2e.py"]

#: pytest-native modules — collected normally, so not run as subprocesses.
#
# Read from conftest rather than restated. This was a second copy of the same
# list, and only conftest's copy actually gates collection: a file added here
# alone satisfied the accounting check below while pytest still ignored it. That
# is how eight pytest-native suites came to pass when run by hand and never run
# in CI at all. One list, one meaning.
from conftest import _PYTEST_NATIVE  # noqa: E402

PYTEST_SUITES = sorted(_PYTEST_NATIVE - {"conftest.py"})


def test_every_suite_is_accounted_for():
    """No test file may fall between the two worlds.

    A legacy script that isn't listed above is neither run by this module nor
    excluded from collection — so pytest imports it, hits its `sys.exit()`, and
    dies with INTERNALERROR before a single test runs. The whole suite goes
    dark, and the message says nothing about the file that caused it.
    """
    known = set(OFFLINE_SUITES) | set(DOCKER_SUITES) | set(KEY_GATED_SUITES) | set(PYTEST_SUITES)
    on_disk = {p.name for p in TESTS.glob("test_*.py")}
    unaccounted = sorted(on_disk - known)
    assert not unaccounted, (
        f"test files in neither list: {unaccounted}. Add a pytest-native module to "
        "PYTEST_SUITES, or a PASS/FAIL script to OFFLINE_SUITES / DOCKER_SUITES."
    )
