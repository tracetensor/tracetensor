"""
Correctness suite — the silent-failure fixes.

Every check here covers a bug whose symptom was *nothing happening*: a malformed
task.toml quietly downgrading the sandbox, a stripped `assert` letting a None
through, a terminal DB write failing with no trace, an event registry growing
without bound. Offline: no Docker, no API key, no server.

Run:  cd backend && python tests/test_correctness.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE.parent))

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


# ---------------------------------------------------------------------------
print("\n== C-2. Malformed task.toml fails the trial instead of silently defaulting ==")
# ---------------------------------------------------------------------------
from app.services.trial_runner import run_trial  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    task_dir = Path(td) / "bad-task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("do a thing")
    (task_dir / "tests" / "test.sh").write_text("echo hi")
    # Declares no-network, but the file is unparseable. Before the fix this ran
    # with the "public" default and looked like a normal result.
    (task_dir / "task.toml").write_text('[environment]\nnetwork_mode = = "no-network"\n')

    out = run_trial(task_dir, "do a thing", agent_name="oracle", backend="local")
    check("status is error", out.status == "error", out.status)
    check("error names task.toml", "task.toml" in (out.error or ""), (out.error or "")[:70])
    check("did not run to completion", out.reward is None and out.passed is None)

with tempfile.TemporaryDirectory() as td:
    # A missing task.toml is the same class of failure — the sandbox contract is
    # unknown, so the trial must not proceed on defaults.
    task_dir = Path(td) / "no-toml"
    task_dir.mkdir()
    out = run_trial(task_dir, "x", agent_name="oracle", backend="local")
    check("missing task.toml -> error", out.status == "error", out.status)


# ---------------------------------------------------------------------------
print("\n== C-1. Runtime guards survive `python -O` (no bare asserts) ==")
# ---------------------------------------------------------------------------
import app.services.agents.llm_agent as agent_mod  # noqa: E402
import app.services.environment as env_mod  # noqa: E402

src_agent = Path(agent_mod.__file__).read_text()
src_env = Path(env_mod.__file__).read_text()
check("llm_agent.py has no bare assert", "\n        assert " not in src_agent)
check("environment.py has no bare assert", "\n        assert " not in src_env)


class _NotDocker:
    """Stand-in for a future non-Docker environment backend."""


try:
    env_mod.DockerEnvironment.transfer_from(
        object.__new__(env_mod.DockerEnvironment), _NotDocker(), []
    )
    check("transfer_from rejects non-Docker source", False)
except TypeError as exc:
    check("transfer_from rejects non-Docker source", "DockerEnvironment" in str(exc))
except Exception as exc:  # any other error means the guard didn't fire first
    check("transfer_from rejects non-Docker source", False, type(exc).__name__)

try:
    agent_mod.LLMAgent("definitely-not-a-provider")
    check("LLMAgent rejects unknown provider", False)
except ValueError:
    check("LLMAgent rejects unknown provider", True)


# ---------------------------------------------------------------------------
print("\n== C-3. A failed terminal write is logged, not swallowed ==")
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
import logging  # noqa: E402

from app.services import executor  # noqa: E402


class _ExplodingSessionMaker:
    def __call__(self):
        raise RuntimeError("db is gone")


records: list[logging.LogRecord] = []


class _Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


_h = _Capture()
executor.log.addHandler(_h)
try:
    import uuid as _uuid

    asyncio.run(executor._fail(_ExplodingSessionMaker(), executor.Job, _uuid.uuid4(), "boom"))
    check("_fail does not raise", True)
finally:
    executor.log.removeHandler(_h)

check("_fail logged the failure", any(r.levelno >= logging.ERROR for r in records))


# ---------------------------------------------------------------------------
print("\n== F-2. In-memory event registry stays bounded under sustained load ==")
# ---------------------------------------------------------------------------
from app.services import event_bus  # noqa: E402

# The memory backend is what the CLI and offline runs use; the server uses the
# database one. Only this backend needs a cap, and its eviction had a bug: when
# every channel was still live the loop deleted nothing but added anyway.
backend = event_bus.MemoryBackend()
event_bus.configure_backend(backend)

for i in range(_n := event_bus._MAX_BUSES * 4):
    event_bus.create_bus(f"live-{i}")
check(
    "registry capped with all-live channels",
    len(backend._events) <= event_bus._MAX_BUSES,
    f"{len(backend._events)} <= {event_bus._MAX_BUSES}",
)
check("newest channel retained", backend.exists(f"live-{_n - 1}"))

# Finished channels are evicted before live ones.
event_bus.clear_all_buses()
for i in range(event_bus._MAX_BUSES - 1):
    event_bus.create_bus(f"done-{i}").emit({"type": "job_done"})
event_bus.create_bus("still-live").emit({"type": "phase", "phase": "agent"})
for i in range(event_bus._MAX_BUSES // 2):
    event_bus.create_bus(f"next-{i}")
check("live channel outlives finished ones", backend.exists("still-live"))
check(
    "registry still capped after mixed churn",
    len(backend._events) <= event_bus._MAX_BUSES,
    str(len(backend._events)),
)

event_bus.clear_all_buses()
check("clear_all_buses empties the registry", backend._events == {})


# ---------------------------------------------------------------------------
print("\n== F-5. In-memory rate limiter does not leak per-client entries ==")
# ---------------------------------------------------------------------------
import time as _time  # noqa: E402

from app.core.security import _MemoryRateLimiter  # noqa: E402

limiter = _MemoryRateLimiter(per_minute=60)
for i in range(10_000):
    limiter.check(f"10.0.{i // 256}.{i % 256}")
check("in-window hits are retained", len(limiter._hits) == 10_000, str(len(limiter._hits)))

# None of those 10k clients ever comes back. Per-key cleanup in check() can't
# reach them — only the periodic sweep can. Age the recorded hits past the
# window and force the sweep clock to be due.
for w in limiter._hits.values():
    w[0] -= 120.0
limiter._next_sweep = _time.monotonic() - 1
limiter.check("a-new-client")
check(
    "one-shot clients are swept once their window expires",
    len(limiter._hits) == 1,
    f"{len(limiter._hits)} entries",
)

# A client still inside its window must survive the sweep.
limiter.check("active-client")
for k, w in limiter._hits.items():
    if k != "active-client":
        w[0] -= 120.0
limiter._next_sweep = _time.monotonic() - 1
limiter.check("active-client")
check("active client survives the sweep", "active-client" in limiter._hits)
check("rate limit still enforced after sweeps", len(limiter._hits["active-client"]) == 2)


# ---------------------------------------------------------------------------
print("\n== C-5. The always-None `backend` field is gone from JobView ==")
# ---------------------------------------------------------------------------
from app.schemas.examine import JobView  # noqa: E402

fields = getattr(JobView, "model_fields", None) or getattr(JobView, "__fields__", {})
check("JobView has no `backend` field", "backend" not in fields)


# ---------------------------------------------------------------------------
print("\n== H-1. Every registered agent is actually constructible ==")
# ---------------------------------------------------------------------------
# The bug this replaces: the constructor lived in a hand-written if/elif chain
# separate from the registry, so an agent could be registered, appear in the UI,
# pass validation — and then fail at runtime with "Unknown agent" because
# somebody forgot the fourth edit. Dispatch now goes through spec.factory, and
# this proves every entry resolves.
from app.services.agents import AGENTS, lookup, make_agent  # noqa: E402
from app.services.agents.base import BaseAgent  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    task_dir = Path(td)
    for name, spec in AGENTS.items():
        try:
            built = spec.factory(task_dir, spec.default_model)
            ok, detail = isinstance(built, BaseAgent), type(built).__name__
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        check(f"{name} constructs a BaseAgent", ok, detail)

    # Aliases must reach the same agent as the canonical name.
    for name, spec in AGENTS.items():
        for alias in spec.aliases:
            check(
                f"alias {alias!r} -> {name}",
                type(make_agent(alias, task_dir)).__name__
                == type(make_agent(name, task_dir)).__name__,
            )

check(
    "every spec has an honest status",
    all(s.status in {"verified", "gated", "unsupported"} for s in AGENTS.values()),
)
check(
    "unsupported agents explain themselves",
    all(s.note for s in AGENTS.values() if s.status == "unsupported"),
)
check(
    "agents needing a model declare a default",
    all(s.default_model for s in AGENTS.values() if s.key_check is not None),
)
check("lookup() resolves every canonical name", all(lookup(n) is AGENTS[n] for n in AGENTS))


# ---------------------------------------------------------------------------
print("\n== A custom agent must actually implement the agent contract ==")
# ---------------------------------------------------------------------------
# `make_agent("pkg:Class")` imports a caller-named class. Nothing had checked it
# was an agent, so a wrong class was accepted here and blew up much later inside
# the trial — where the failure reads as a broken task rather than a broken
# configuration. Surfaced by turning on mypy's warn_return_any.
import sys as _sys  # noqa: E402
import types as _types  # noqa: E402

from app.services.agents import make_agent  # noqa: E402
from app.services.agents.base import AgentResult, BaseAgent  # noqa: E402

_probe = _types.ModuleType("_tt_probe_agents")


class _NotAnAgent:
    def __init__(self, model=None):
        pass


class _RealAgent(BaseAgent):
    name = "probe"

    def __init__(self, model=None):
        self.model = model

    def run(self, instruction, env, timeout=None, on_event=None):
        return AgentResult([])


_probe._NotAnAgent = _NotAnAgent
_probe._RealAgent = _RealAgent
_sys.modules["_tt_probe_agents"] = _probe

try:
    make_agent("_tt_probe_agents:_NotAnAgent", Path("."))
    check("a non-BaseAgent custom class is rejected", False)
except ValueError as exc:
    check("a non-BaseAgent custom class is rejected", "BaseAgent" in str(exc), str(exc)[:70])

check(
    "a real BaseAgent subclass still loads",
    isinstance(make_agent("_tt_probe_agents:_RealAgent", Path(".")), BaseAgent),
)

try:
    make_agent("_tt_probe_agents:NoSuchClass", Path("."))
    check("a missing class is a clean error", False)
except ValueError as exc:
    check("a missing class is a clean error", "Could not load" in str(exc))

del _sys.modules["_tt_probe_agents"]


print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
