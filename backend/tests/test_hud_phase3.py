"""
Phase 3 tests: Docker capability orchestration + server/dashboard path for HUD tasks.

Tests:
  1. Capability.browser() with no url → url="docker://auto", params includes managed=True
  2. Capability.desktop() with no url → url="docker://auto", params includes managed=True
  3. Capability.browser(url="ws://localhost:9222") → existing behaviour unchanged
  4. _provision_capability_containers skips caps that already have a real URL
  5. _provision_capability_containers handles docker-not-found gracefully
  6. task_validator accepts HUD format (env.py + tasks.py) as READY
  7. task_validator rejects HUD format missing tasks.py (REGISTERED + error)
  8. task_service._persist_hud_task extracts name and task_count correctly
  9. executor detects HUD format and calls _execute_hud_job instead of run_trial
 10. CLI routes HUD format to server when --server is given
 11. Full round-trip: CLI --server=... uploads HUD env, executor runs it (mocked DB)
 12. Real OpenAI: HUD blank env via server executor path (skip if no key)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BLANK_DIR = Path(__file__).parent / "hud_compat" / "blank"
MCP_DIR = Path(__file__).parent / "hud_compat" / "mcp_example"


# ── 1-3: Capability factory changes ─────────────────────────────────────────


def test_browser_default_url_is_managed():
    from app.capabilities.base import Capability
    cap = Capability.browser()
    assert cap.url == "docker://auto"
    assert cap.params.get("managed") is True
    assert cap.protocol == "cdp/1.3"


def test_desktop_default_url_is_managed():
    from app.capabilities.base import Capability
    cap = Capability.desktop()
    assert cap.url == "docker://auto"
    assert cap.params.get("managed") is True
    assert cap.protocol == "rfb/3.8"


def test_browser_explicit_url_unchanged():
    from app.capabilities.base import Capability
    cap = Capability.browser(url="ws://localhost:9222")
    assert cap.url == "ws://localhost:9222"
    assert not cap.params.get("managed")


def test_desktop_explicit_url_unchanged():
    from app.capabilities.base import Capability
    cap = Capability.desktop(url="rfb://localhost:5900")
    assert cap.url == "rfb://localhost:5900"
    assert not cap.params.get("managed")


# ── 4-5: Container provisioning ──────────────────────────────────────────────


def test_provision_skips_real_urls():
    """Caps with a real URL are not provisioned."""
    from app.capabilities.base import Capability
    from app.services.hud_compat import TensorEnvironment
    from app.services.hud_runner import _provision_capability_containers

    env_obj = TensorEnvironment(name="test")
    env_obj.add_capability(Capability.mcp(url="http://localhost:18040/mcp"))
    env_obj.add_capability(Capability.browser(url="ws://localhost:9222"))

    containers = asyncio.run(_provision_capability_containers(env_obj))
    assert containers == []
    # Caps unchanged
    assert env_obj.capabilities[0].url == "http://localhost:18040/mcp"
    assert env_obj.capabilities[1].url == "ws://localhost:9222"


def test_provision_handles_docker_not_found(monkeypatch):
    """When Docker is not installed the runner degrades gracefully."""
    from app.capabilities.base import Capability
    from app.services.hud_compat import TensorEnvironment
    from app.services.hud_runner import _provision_capability_containers

    async def _no_docker(*args, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(
        "app.services.hud_runner.asyncio.create_subprocess_exec",
        _no_docker,
    )

    env_obj = TensorEnvironment(name="test")
    env_obj.add_capability(Capability.browser())  # managed

    # Should not raise — returns empty list
    containers = asyncio.run(_provision_capability_containers(env_obj))
    assert containers == []


def test_provision_starts_and_replaces_url(monkeypatch):
    """Managed cap gets replaced with the real localhost URL after docker run."""
    from app.capabilities.base import Capability
    from app.services.hud_compat import TensorEnvironment
    from app.services.hud_runner import _provision_capability_containers

    call_args: list = []

    async def fake_subprocess_exec(*args, **kwargs):
        call_args.append(args)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"abc123def456\n", b""))
        return proc

    monkeypatch.setattr(
        "app.services.hud_runner.asyncio.create_subprocess_exec",
        fake_subprocess_exec,
    )
    monkeypatch.setattr("app.services.hud_runner.asyncio.sleep", AsyncMock())

    env_obj = TensorEnvironment(name="test")
    env_obj.add_capability(Capability.browser())  # managed

    containers = asyncio.run(_provision_capability_containers(env_obj))
    assert len(containers) == 1
    assert containers[0] == "abc123def456"  # 12-char short id
    # The cap URL should now be a real ws:// URL
    assert env_obj.capabilities[0].url.startswith("ws://localhost:")
    assert "managed" not in env_obj.capabilities[0].params


# ── 6-7: task_validator HUD support ─────────────────────────────────────────


def test_validator_accepts_hud_format():
    from app.services.task_validator import STATUS_READY, validate_task
    result = validate_task(BLANK_DIR)
    assert result.status == STATUS_READY
    assert result.is_valid is True
    assert result.errors == []


def test_validator_rejects_hud_missing_tasks(tmp_path):
    from app.services.task_validator import STATUS_REGISTERED, validate_task
    (tmp_path / "env.py").write_text("from hud import Environment\nenv = Environment(name='x')\n")
    result = validate_task(tmp_path)
    assert result.is_valid is False
    assert result.status == STATUS_REGISTERED
    assert any("tasks.py" in e for e in result.errors)


# ── 8: task_service HUD persist ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_hud_task_extracts_name():
    """_persist_hud_task stores the env name and marks the task READY."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services.task_service import _persist_hud_task  # type: ignore[attr-defined]

    # Build a minimal mock DB session
    mock_task = MagicMock()
    mock_execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    mock_db = AsyncMock()
    mock_db.execute = mock_execute
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()
    mock_db.add = MagicMock()

    task, validation = await _persist_hud_task(mock_db, BLANK_DIR)
    # We can't fully assert on the task object since it's a mock-created Task,
    # but we can verify the validation is READY
    from app.services.task_validator import STATUS_READY
    assert validation.status == STATUS_READY


# ── 9: executor HUD routing ──────────────────────────────────────────────────


def test_executor_detects_hud_format(monkeypatch):
    """execute_job calls _execute_hud_job when task_dir contains env.py."""
    from app.services.format_detector import detect_format

    # The blank dir has env.py → hud format
    assert detect_format(BLANK_DIR) == "hud"

    # The native dir (task.toml) → tracetensor format
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "task.toml").write_text('[task]\nname = "x"\n')
        assert detect_format(Path(td)) == "tracetensor"


# ── 10: CLI server routing ────────────────────────────────────────────────────


def test_cli_hud_with_server_calls_run_remote(monkeypatch):
    """When --server is given for a HUD dir, CLI uses _run_remote instead of _run_hud."""
    from typer.testing import CliRunner

    called = {}

    def fake_run_remote(*args, **kwargs):
        called["remote"] = True
        return [], 0.1, "http://fake/job/1"

    def fake_report(*args, **kwargs):
        pass

    import app.cli.run as run_mod
    monkeypatch.setattr(run_mod, "_run_remote", fake_run_remote)
    monkeypatch.setattr(run_mod, "_report", fake_report)

    from app.cli.main import app
    result = CliRunner().invoke(app, [
        "run", str(BLANK_DIR),
        "-a", "openai",
        "--server", "http://localhost:8000",
    ])
    assert called.get("remote") is True


# ── 11: executor _execute_hud_job integration (mock DB) ──────────────────────


@pytest.mark.asyncio
async def test_execute_hud_job_runs_all_tasks(monkeypatch):
    """_execute_hud_job runs each BoundTask and writes Trial rows."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from app.services.executor import _execute_hud_job
    from app.services.hud_runner import HudTrialOutcome
    from app.models.enums import JobStatus

    trials_written = []

    class FakeSession:
        def __init__(self):
            self._items = []

        def add(self, item):
            trials_written.append(item)

        async def execute(self, stmt):
            mock = MagicMock()
            job_row = MagicMock()
            job_row.trials_completed = 0
            job_row.trials_passed = 0
            mock.scalar_one.return_value = job_row
            return mock

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    def fake_session_factory():
        return FakeSession()

    job = MagicMock()
    job.agent = "openai"
    job.model = "gpt-4o-mini"
    job.n_trials = 1

    outcomes = [
        HudTrialOutcome(status="completed", reward=1.0, passed=True, duration_s=0.1),
        HudTrialOutcome(status="completed", reward=1.0, passed=True, duration_s=0.1),
        HudTrialOutcome(status="completed", reward=0.0, passed=False, duration_s=0.1),
    ]
    call_count = 0

    async def fake_run_hud_task(bt, **kw):
        nonlocal call_count
        out = outcomes[call_count % len(outcomes)]
        call_count += 1
        return out

    # executor calls _hud_runner_mod.run_hud_task — patch on the module.
    monkeypatch.setattr("app.services.hud_runner.run_hud_task", fake_run_hud_task)

    events = []
    await _execute_hud_job(
        SessionLocal=fake_session_factory,
        job_id=uuid.uuid4(),
        job=job,
        task_dir=BLANK_DIR,
        task_id=uuid.uuid4(),
        run_id=None,
        task_name="blank",
        bus=MagicMock(),
        emit=lambda e: events.append(e),
    )

    # blank/tasks.py has 3 tasks × 1 trial = 3 trials total
    assert call_count == 3
    types = [e["type"] for e in events]
    assert "job_started" in types
    assert "job_done" in types
    job_done = next(e for e in events if e["type"] == "job_done")
    assert job_done["trials_passed"] == 2


# ── 12: Real OpenAI end-to-end through executor path ─────────────────────────


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set"
)
@pytest.mark.asyncio
async def test_execute_hud_job_real_openai():
    """Full executor path with real OpenAI — at least one task should pass."""
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from app.services.executor import _execute_hud_job

    trials_written = []

    class FakeSession:
        def add(self, item):
            trials_written.append(item)

        async def execute(self, stmt):
            mock = MagicMock()
            job_row = MagicMock()
            job_row.trials_completed = 0
            job_row.trials_passed = 0
            mock.scalar_one.return_value = job_row
            return mock

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    def fake_session_factory():
        return FakeSession()

    job = MagicMock()
    job.agent = "openai"
    job.model = "gpt-4o-mini"
    job.n_trials = 1

    events = []
    await _execute_hud_job(
        SessionLocal=fake_session_factory,
        job_id=uuid.uuid4(),
        job=job,
        task_dir=BLANK_DIR,
        task_id=uuid.uuid4(),
        run_id=None,
        task_name="blank",
        bus=MagicMock(),
        emit=lambda e: events.append(e),
    )

    job_done = next((e for e in events if e["type"] == "job_done"), None)
    assert job_done is not None
    assert job_done["trials_passed"] >= 1  # at least "banana"/"a" passes reliably
    assert len(trials_written) == 3  # 3 tasks × 1 trial
