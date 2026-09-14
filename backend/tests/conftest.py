"""
Shared pytest fixtures and fakes.

The point of this file is that the expensive things — Docker, a model API, a
database — have stand-ins. A test that wants to check how `run_trial` sequences
its phases, or what `LLMAgent` does when the model never emits a command, should
not need a container and a paid API call to find out. Before the factory seams
existed those tests were simply not written.

Fakes live here rather than in individual test modules so there is one place to
look for "what does a test environment/agent/model do", and one place to fix
when the real contract changes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
PROJECT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Point the suite at a throwaway SQLite file BEFORE any app module is imported.
# app.core.database builds its engine at import time from settings.DATABASE_URL,
# so this can't be a fixture — by the time one ran, the engine would already
# exist. Without it the default is the docker-compose Postgres, which means the
# suite either fails on a laptop with no Postgres or, worse, writes to a real
# database. An explicit DATABASE_URL (CI, or a developer testing against
# Postgres on purpose) is left alone.
if not os.environ.get("DATABASE_URL"):
    _TEST_DB = Path(tempfile.mkdtemp(prefix="tracetensor-tests-")) / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"
    os.environ.setdefault("TASKS_ROOT", str(_TEST_DB.parent / "tasks"))

# No embedded worker inside the pytest process. An in-process worker would claim
# any job these tests create and start running real Docker containers underneath
# them — slow, and it makes results depend on whether a daemon happens to be up.
# The suites that genuinely want execution (the Docker tier, the browser smoke
# tests) launch their own server with their own environment.
os.environ.setdefault("WORKER_EMBEDDED", "false")

from app.services.environment import BaseEnvironment, ExecResult  # noqa: E402

EXAMPLES = PROJECT / "examples"


# Key-gated suites spend real money; they are never collected.
_KEY_GATED = {"test_phase2_e2e.py", "test_phase2_api_e2e.py"}

# The pytest-native suites — the only files pytest should import and collect
# directly. Everything else under tests/ is a script suite (runs its body at
# import, calls sys.exit) and MUST be ignored, or that sys.exit crashes the
# whole collection with an opaque INTERNALERROR before a single test runs.
#
# Sourced by exclusion, not by an allow-list: a new script suite that nobody
# registered is ignored by default (safe), and test_legacy_suites.py's
# `test_every_suite_is_accounted_for` is what then fails loudly to say "add it".
# The previous version listed what to ignore, so an unlisted script slipped
# through and took collection down — twice, both times from files added
# mid-session by another editor.
_PYTEST_NATIVE = {
    "test_agent_hardening.py",
    "test_agent_instrumentation.py",
    "test_api_contract.py",
    "test_bash_agent_prompt.py",
    "test_backend_capabilities.py",
    "test_backend_catalog.py",
    "test_dataset_service.py",
    "test_daytona_runtime.py",
    "test_diagnose.py",
    "test_environment_platform.py",
    "test_environment_registry.py",
    "test_frontend_contract.py",
    "test_frontend_smoke.py",
    "test_job_service.py",
    "test_langgraph_agent.py",
    "test_legacy_suites.py",
    "test_llm_agent_unit.py",
    "test_local_preflight.py",
    "test_routers_unit.py",
    "test_trial_runner_unit.py",
    "conftest.py",
}

collect_ignore = sorted(
    p.name for p in Path(__file__).parent.glob("test_*.py") if p.name not in _PYTEST_NATIVE
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_globals():
    """Reset process-global state between tests.

    The event-bus registry is a module-level dict, so without this a test that
    creates buses changes what the next test sees and the suite becomes
    order-dependent. Autouse because the failure it prevents is invisible: tests
    pass alone and fail in a full run, or vice versa.
    """
    from app.services import event_bus

    # Snapshot and restore the backend too, not just the channel state: a test
    # (or a TestClient lifespan) that installs the database backend would
    # otherwise leave it installed, and the next in-memory test would read from
    # a table nothing wrote to — passing alone, failing in a full run.
    previous = event_bus.current_backend()
    event_bus.clear_all_buses()
    yield
    event_bus.clear_all_buses()
    event_bus.configure_backend(previous)


@pytest.fixture
def settings_override():
    """Pin settings for one test, restored afterwards.

    def test_x(settings_override):
        with settings_override(RATE_LIMIT_PER_MINUTE=2):
            ...
    """
    from app.core import config

    return config.override


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEnvironment(BaseEnvironment):
    """An environment that records commands instead of running them.

    Implements the full BaseEnvironment contract — it has to, since the ABC now
    rejects a partial implementation, which is the point of that change.

    `responses` maps a substring of a command to the ExecResult to return, so a
    test can say "when the agent runs anything containing `pytest`, exit 1".
    Anything unmatched succeeds silently.
    """

    def __init__(self, task_dir: Path = Path("."), *, responses: dict | None = None, **kwargs):
        self.task_dir = task_dir
        self.responses = responses or {}
        self.files: dict[str, str] = {}
        self.commands: list[str] = []
        self.networks: list = []
        self.setup_called = False
        self.teardown_called = False
        self.transferred: list = []
        self.missing_artifacts: list = []
        self.kwargs = kwargs

    def build(self) -> None:
        pass

    def setup(self) -> None:
        self.setup_called = True

    def exec(self, command, phase="agent", timeout=None, as_user=None, env=None) -> ExecResult:
        self.commands.append(command)
        for needle, result in self.responses.items():
            if needle in command:
                return ExecResult(
                    command=command,
                    exit_code=result.get("exit_code", 0),
                    stdout=result.get("stdout", ""),
                    stderr=result.get("stderr", ""),
                    duration_s=0.0,
                    phase=phase,
                )
        return ExecResult(
            command=command, exit_code=0, stdout="", stderr="", duration_s=0.0, phase=phase
        )

    def read_file(self, path: str):
        return self.files.get(path)

    def write_file(self, path: str, content: bytes) -> None:
        self.files[path] = content.decode(errors="replace")

    def copy_in(self, src_dir: Path, dest: str) -> None:
        pass

    def set_network(self, mode, *, allowed_hosts=()) -> None:
        self.networks.append(mode)

    def transfer_from(self, other, paths):
        self.transferred.extend(paths)
        # Report the caller's declared-missing set so the artifact warnings in
        # run_trial can be exercised without a real container.
        return [p for p in paths if p in self.missing_artifacts]

    def teardown(self) -> None:
        self.teardown_called = True


class FakeAgent:
    """An agent that runs a scripted list of commands and then stops."""

    def __init__(self, commands=(), error=None, llm_calls=None, name="fake"):
        self.name = name
        self.PROMPT_VERSION = "fake"
        self._commands = list(commands)
        self._error = error
        self._llm_calls = llm_calls or []

    def run(self, instruction, env, timeout=None, on_event=None, setup_timeout=None):
        from app.services.agents.base import AgentResult

        steps = [env.exec(c, phase="agent") for c in self._commands]
        return AgentResult(
            steps,
            error=self._error,
            llm_calls=self._llm_calls,
            prompt_version=self.PROMPT_VERSION,
        )


class ScriptedLLM:
    """Stands in for `llm.call_llm`, returning replies in order.

    Records every (system, history) it was handed so a test can assert on what
    the model actually saw — which is how the instruction-truncation and
    history-trimming guards get verified without paying for a call.
    """

    def __init__(self, replies, usage=None):
        self.replies = list(replies)
        self.calls: list = []
        self.usage = usage or {}

    def __call__(self, provider, model, system, history):
        from app.services.llm import LLMCallResult

        self.calls.append(
            {"provider": provider, "model": model, "system": system, "history": history}
        )
        text = self.replies.pop(0) if self.replies else "DONE"
        if isinstance(text, Exception):
            raise text
        return LLMCallResult(
            text=text,
            provider=provider,
            model=model,
            input_tokens=self.usage.get("input_tokens", 10),
            output_tokens=self.usage.get("output_tokens", 5),
            latency_ms=self.usage.get("latency_ms", 1.0),
            cost_usd=self.usage.get("cost_usd"),
        )


@pytest.fixture
def fake_env():
    return FakeEnvironment()


@pytest.fixture
def sort_csv_task():
    """The bundled example task — a real, valid task directory on disk."""
    return EXAMPLES / "sort-csv"


@pytest.fixture
def task_dir(tmp_path):
    """A minimal valid task directory, for tests that only need one to exist."""
    d = tmp_path / "sample-task"
    (d / "tests").mkdir(parents=True)
    (d / "environment").mkdir(parents=True)
    (d / "instruction.md").write_text("Do the thing.")
    (d / "tests" / "test.sh").write_text("echo ok")
    (d / "environment" / "Dockerfile").write_text("FROM python:3.11-slim")
    (d / "task.toml").write_text(
        'schema_version = "1.3"\n\n'
        '[task]\nname = "test/sample"\n\n'
        '[environment]\nnetwork_mode = "no-network"\n'
    )
    return d


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """An isolated SQLite database with the real schema, per test.

    Builds its OWN engine rather than reusing app.core.database's: that one is
    constructed at import time from DATABASE_URL, so it points wherever the
    developer's environment happens to point. A per-test file means tests can't
    see each other's rows and can't touch a real database by accident.

    Tables come from the ORM metadata rather than from Alembic — migration
    fidelity is tests/test_schema.py's job, and paying for a migration run per
    test would make this fixture too slow to use freely.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base, import_all_models

    import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def ready_task(db, task_dir):
    """A registered, runnable task row."""
    from app.services.task_service import persist_task

    task, _validation = await persist_task(db, task_dir)
    return task
