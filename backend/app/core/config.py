"""Application configuration."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Load backend/.env (if present) so API keys and DATABASE_URL can live in a
# file the user controls rather than being exported by hand each run. This is
# best-effort: python-dotenv ships with uvicorn[standard], but we don't hard-fail
# if it's missing.
try:
    from dotenv import load_dotenv

    _ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(_ENV_FILE)
except Exception:  # pragma: no cover - dotenv optional
    pass


def _secret(name: str, default: str | None = None) -> str | None:
    """Read a secret, preferring a file over a plain env var.

    Follows the Docker-secrets convention: if `<NAME>_FILE` is set, the secret is
    read from that file (e.g. /run/secrets/<name>) — so keys never appear in the
    environment / `docker inspect`. Otherwise fall back to the `<NAME>` env var,
    then `default`. Whitespace/newline is stripped (secret files usually end in one).
    """
    file_path = os.getenv(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text().strip()
        except OSError:
            pass
    return os.getenv(name, default)


class Settings:
    """Runtime configuration, read from the environment (and backend/.env).

    Fields are assigned in __init__ rather than as class attributes so the object
    can be rebuilt from a changed environment. That is what `reload()` and
    `override()` below need, and it is why tests can now pin a setting instead of
    inheriting whatever the developer happened to export.

    Not pydantic-settings: config.py is imported by the offline test tier, which
    runs against a pydantic shim and has no pydantic-settings available. The
    validation would be nice; being able to run the logic suites with no
    third-party packages at all is worth more here.
    """

    def __init__(self) -> None:
        # Where uploaded task directories are stored on disk.
        self.TASKS_ROOT: Path = Path(os.getenv("TASKS_ROOT", "/data/tasks"))

        # Database URL. Defaults to local Postgres from docker-compose.
        # For a quick zero-Postgres run, set DATABASE_URL to a sqlite+aiosqlite URL.
        self.DATABASE_URL: str = _secret(
            "DATABASE_URL",
            "postgresql+asyncpg://tracetensor:tracetensor@localhost:5432/tracetensor",
        )  # type: ignore[assignment]  # default is non-None, so this is always str

        # Connection pool budget (Postgres). Keep pool_size + max_overflow under the
        # server's max_connections; put PgBouncer in front for many app instances.
        self.DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "10"))
        self.DB_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "20"))
        self.DB_POOL_TIMEOUT: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
        self.DB_POOL_RECYCLE: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # recycle stale conns

        # CORS origins for the frontend dev server.
        self.CORS_ORIGINS: list[str] = os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://localhost:3000,http://localhost:8000"
        ).split(",")

        self.APP_NAME: str = "TraceTensor"
        self.APP_VERSION: str = "0.1.0"

        # ---- Access control ----------------------------------------------------
        # Optional shared secret. UNSET (default) = no auth, so localhost/dev works
        # with zero config. SET = every mutating/execute endpoint requires it via
        # `Authorization: Bearer <token>` or `X-API-Key: <token>`. Set this the
        # moment the API is reachable beyond localhost — without it, anyone who can
        # reach the port can start jobs that spend your LLM budget.
        self.API_TOKEN: str | None = _secret("API_TOKEN") or None

        # Rate limit for job-creating endpoints (per client, per minute).
        # Default 60 req/min per client — a backstop against runaway loops racking up
        # API bills. Set to 0 to disable (not recommended on networked deployments).
        self.RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))

        # ---- Job queue / workers ------------------------------------------------
        # Jobs and dataset runs are a durable queue in Postgres (the jobs/dataset_runs
        # tables); workers claim queued work with SELECT ... FOR UPDATE SKIP LOCKED,
        # so any number of workers on any number of machines can share the load.
        #
        # WORKER_EMBEDDED: run a worker inside the web process. True (default) keeps
        # single-node deployments zero-config — the API also executes jobs, exactly
        # like before. Set False on the web tier and run standalone workers
        # (`python -m app.worker`) elsewhere to scale execution across machines.
        self.WORKER_EMBEDDED: bool = os.getenv("WORKER_EMBEDDED", "true").lower() in (
            "1",
            "true",
            "yes",
        )
        # How many jobs/runs one worker executes concurrently (each still fans its
        # own trials out internally).
        self.WORKER_CONCURRENCY: int = int(os.getenv("WORKER_CONCURRENCY", "4"))
        # Idle poll interval (seconds) between claim attempts when the queue is empty.
        self.WORKER_POLL_INTERVAL: float = float(os.getenv("WORKER_POLL_INTERVAL", "0.5"))
        # A running item whose worker hasn't heartbeated within this many seconds is
        # considered dead; its lease is reclaimed (marked failed) so it never wedges.
        self.WORKER_LEASE_TIMEOUT: int = int(os.getenv("WORKER_LEASE_TIMEOUT", "180"))
        # How often a running item refreshes its lease.
        self.WORKER_HEARTBEAT_INTERVAL: float = float(os.getenv("WORKER_HEARTBEAT_INTERVAL", "20"))
        # Stable id for this worker (defaults to host:pid). Shows up in logs and the
        # worker_id column so you can see which machine ran what.
        self.WORKER_ID: str = os.getenv("WORKER_ID", "")
        # On shutdown, how long to wait for the embedded worker's in-flight jobs to
        # finish before exiting anyway (they'd then be reclaimed). A bounded graceful
        # drain — long enough for quick jobs, short enough not to hang a deploy.
        self.WORKER_DRAIN_TIMEOUT: float = float(os.getenv("WORKER_DRAIN_TIMEOUT", "30"))

        # ---- Agent budgets -------------------------------------------------------
        # Ceilings handed to the agent for a single trial. These were hardcoded class
        # constants, which meant tuning your LLM spend meant editing source. An
        # operator running a large dataset needs to lower them; someone evaluating a
        # hard task needs to raise them.
        #
        # AGENT_COST_LIMIT_USD is passed to agents that accept a spend cap (mini-swe's
        # `-l`, Claude Code's `--max-budget-usd`) and is per trial, not per job — a
        # 20-trial job can spend up to 20x this.
        self.AGENT_COST_LIMIT_USD: float = float(os.getenv("AGENT_COST_LIMIT_USD", "1.0"))
        # Turn cap for installed agents that accept one (Claude Code's --max-turns).
        self.AGENT_MAX_TURNS: int = int(os.getenv("AGENT_MAX_TURNS", "40"))
        # Command budget for the built-in bash loop (app.services.agents.llm_agent).
        # Lower than the installed agents' turn caps because each step here is a full
        # model round-trip that we pay for directly. 200 steps matches Harbor's
        # agent budget and avoids agents timing out on real SWE tasks.
        self.LLM_AGENT_MAX_STEPS: int = int(os.getenv("LLM_AGENT_MAX_STEPS", "200"))
        # Per-command cap for the built-in bash loop's agent-phase commands.
        # Without this, a single command is allowed to run for whatever time is
        # LEFT in the whole session (see llm_agent.py's `remaining` timeout) — a
        # command that hangs or is pathologically slow (e.g. an unscoped
        # `grep -r /`) can silently consume nearly the entire trial with no
        # other step getting a chance to run. 300s matches the precedent used by
        # other agentic benchmarks (e.g. scBench's per-command cap) for the same
        # reason. Does not apply to the verifier's test.sh run, which is a
        # separate exec() call under phase="verifier" and needs to run to
        # completion for grading to be meaningful.
        self.LLM_AGENT_COMMAND_TIMEOUT_SEC: float = float(
            os.getenv("LLM_AGENT_COMMAND_TIMEOUT_SEC", "500")
        )

        # Loop-level retry for transient LLM provider errors (overload, rate
        # limit, 5xx, connection drop) that survive the SDK client's own
        # max_retries (LLM_TIMEOUT_SEC/LLM_MAX_RETRIES in app.services.llm) — a
        # sustained overload window can outlast a couple of SDK-level retries
        # with short backoff. Without this, ANY exception from the LLM call
        # (transient or not) ends the whole trial immediately, discarding every
        # step already taken. Only errors classified as transient are retried
        # (see app.services.agents.base.is_transient_llm_error) — auth/bad-request/
        # permission errors fail fast as before, since retrying those never helps.
        self.LLM_AGENT_TRANSIENT_RETRY_MAX: int = int(
            os.getenv("LLM_AGENT_TRANSIENT_RETRY_MAX", "5")
        )
        self.LLM_AGENT_TRANSIENT_RETRY_BACKOFF_SEC: float = float(
            os.getenv("LLM_AGENT_TRANSIENT_RETRY_BACKOFF_SEC", "15")
        )

        # ---- Diagnose (docs/DIAGNOSE.md) -----------------------------------------
        # Rule-based failure classification after each trial. Free and offline,
        # so it defaults on; it only annotates the trajectory, never the grade.
        self.DIAGNOSE_ENABLED: bool = os.getenv("DIAGNOSE_ENABLED", "true").lower() in (
            "1",
            "true",
            "yes",
        )
        # The LLM extraction pass costs a model call per failed trial, so it is
        # opt-in — same stance as everything else that spends money.
        self.DIAGNOSE_LLM_ENABLED: bool = os.getenv("DIAGNOSE_LLM_ENABLED", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # provider/model for the extractor; bare model assumes openai.
        self.DIAGNOSE_MODEL: str | None = os.getenv("DIAGNOSE_MODEL")

        # ---- LangGraph adapter ---------------------------------------------------
        # The agent under test lives OUTSIDE the task: a project directory holding a
        # langgraph.json registry plus the code it points at. Harbor passes this as
        # --project-path; we take it from the environment because `make_agent` only
        # receives (task_dir, model) and the project is a property of the run, not
        # the task. LANGGRAPH_GRAPH picks one entry when the registry declares
        # several; unset means "the first one declared".
        self.LANGGRAPH_PROJECT: str | None = os.getenv("LANGGRAPH_PROJECT")
        self.LANGGRAPH_GRAPH: str | None = os.getenv("LANGGRAPH_GRAPH")
        # Graph steps before LangGraph raises GraphRecursionError. A ReAct agent
        # spends ~2 nodes per tool call, so this is roughly half the tool budget.
        self.LANGGRAPH_RECURSION_LIMIT: int = int(os.getenv("LANGGRAPH_RECURSION_LIMIT", "50"))

        # How long live-progress events are kept before pruning. Generously
        # longer than a stream can stay open (it self-terminates at twice the
        # lease timeout), but bounded — a 100-task dataset run emits thousands
        # of markers and the log would otherwise grow forever.
        self.EVENT_RETENTION_SECONDS: int = int(os.getenv("EVENT_RETENTION_SECONDS", "86400"))

        # How much of each task file to inline in a job view (chars). The job
        # log embeds instruction.md, test.sh, solve.sh and the Dockerfile so a
        # result is self-documenting; a task with a large data file would
        # otherwise make every job response enormous.
        self.CONTEXT_FILE_CAP_CHARS: int = int(os.getenv("CONTEXT_FILE_CAP_CHARS", "6000"))

        # Allow the API to instantiate a caller-supplied agent class by import path
        # ("mypkg.agents:MyAgent"). That is arbitrary code execution in the server
        # process, so it's OFF by default: fine for a local CLI run where you are the
        # only caller, not fine for anything networked. The CLI passes such specs
        # straight to make_agent() and is unaffected by this setting.
        self.ALLOW_CUSTOM_AGENTS: bool = os.getenv("ALLOW_CUSTOM_AGENTS", "false").lower() in (
            "1",
            "true",
            "yes",
        )

        # LLM provider API keys (read from env / .env). Absent = provider disabled.
        self.ANTHROPIC_API_KEY: str | None = _secret("ANTHROPIC_API_KEY")
        self.OPENAI_API_KEY: str | None = _secret("OPENAI_API_KEY")
        self.OPENROUTER_API_KEY: str | None = _secret("OPENROUTER_API_KEY")

        # ---- Docker / trial resilience -----------------------------------------
        # Retries for setup-only infra failures (Docker build/pull flake). Never
        # re-runs a trial after the agent phase started (avoids double LLM spend).
        self.INFRA_RETRIES: int = int(os.getenv("TRACETENSOR_INFRA_RETRIES", "1"))
        # Optional docker --platform default (e.g. linux/amd64). "native" = host default.
        _plat = (os.getenv("TRACETENSOR_DOCKER_PLATFORM") or "").strip()
        self.DOCKER_PLATFORM_DEFAULT: str | None = (
            None if not _plat or _plat.lower() in ("native", "host", "none") else _plat
        )

    def available_providers(self) -> dict[str, bool]:
        """Which LLM providers have a key configured (drives the UI selector)."""
        return {
            "anthropic": bool(self.ANTHROPIC_API_KEY),
            "openai": bool(self.OPENAI_API_KEY),
            "openrouter": bool(self.OPENROUTER_API_KEY),
        }


settings = Settings()


def reload() -> "Settings":
    """Rebuild the singleton from the current environment, in place.

    Mutates the existing object rather than rebinding the name, because modules
    all over the app did `from app.core.config import settings` and hold a
    reference to *this* object. Rebinding would leave them on the stale one.
    """
    fresh = Settings()
    settings.__dict__.clear()
    settings.__dict__.update(fresh.__dict__)
    return settings


@contextmanager
def override(**values: object) -> Iterator["Settings"]:
    """Temporarily pin settings, then restore. For tests.

    Nothing forces a caller to clean up after mutating a global, so tests that
    tweaked settings used to leak into whichever test ran next. This restores
    the previous values even if the body raises.

        with config.override(RATE_LIMIT_PER_MINUTE=2, API_TOKEN="t"):
            ...

    Note this does NOT rebuild anything already constructed from a setting — the
    database engine is built at import from DATABASE_URL, so overriding that
    afterwards has no effect. Set DATABASE_URL in the environment before import
    (which is what tests/test_schema.py does with subprocesses).
    """
    unset = object()
    previous = {k: settings.__dict__.get(k, unset) for k in values}
    settings.__dict__.update(values)
    try:
        yield settings
    finally:
        for k, old in previous.items():
            if old is unset:
                settings.__dict__.pop(k, None)
            else:
                settings.__dict__[k] = old


# TASKS_ROOT is a SERVER concern (where uploaded task ZIPs land — see
# app.storage.task_store); run_trial()/the CLI take an explicit task_dir and
# never touch it. Best-effort creation, like the dotenv load above: importing
# `tracetensor` as a plain library must not crash just because the server's
# default path (/data/tasks) isn't writable/doesn't exist on this machine. If
# the server genuinely needs it, task_store's own write will surface a real,
# actionable error at the point of use.
try:
    settings.TASKS_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
