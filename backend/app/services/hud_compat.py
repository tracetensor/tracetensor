"""
TraceTensor stub for the hud.Environment API.

Injected into sys.modules as 'hud' and 'hud.environment' before an env.py
file is imported. Captures @env.template, @env.initialize, @env.shutdown,
env.workspace(), and env.add_capability() calls — without running HUD's
actual server or requiring the hud package.

After import, inspect env_obj.templates, env_obj.init_hooks,
env_obj.shutdown_hooks, env_obj.capabilities.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

from app.capabilities.base import Capability

log = logging.getLogger("tracetensor.hud_compat")


@dataclass
class BoundTask:
    """A concrete, runnable task — one call to a template with real args."""
    template_id: str
    fn: Callable[..., AsyncGenerator]
    kwargs: dict[str, Any]
    env: "TensorEnvironment"

    async def run_generator(self) -> AsyncGenerator:
        """Instantiate the async generator with its bound args."""
        return self.fn(**self.kwargs)


class TensorEnvironment:
    """
    Stub drop-in for hud.Environment. Captures registrations made in env.py
    and exposes them to TraceTensor's HUD adapter.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.templates: dict[str, Callable] = {}   # id → async generator fn
        self.init_hooks: list[Callable] = []
        self.shutdown_hooks: list[Callable] = []
        self.capabilities: list[Capability] = []
        self._workspace_root: Path | None = None

    # ── @env.template(id="...") ──────────────────────────────────────

    def template(self, *, id: str | None = None, **_kwargs: Any) -> Callable:
        """Register an async generator function as a task template.

        Returns a callable that, when called with real args, produces a BoundTask.
        """
        def decorator(fn: Callable) -> Callable:
            template_id = id or fn.__name__
            self.templates[template_id] = fn

            def make_task(**kwargs: Any) -> BoundTask:
                return BoundTask(
                    template_id=template_id,
                    fn=fn,
                    kwargs=kwargs,
                    env=self,
                )

            # Mirror the positional+keyword call pattern HUD uses
            def make_task_any(*args: Any, **kwargs: Any) -> BoundTask:
                sig = inspect.signature(fn)
                params = list(sig.parameters.keys())
                bound: dict[str, Any] = {}
                for i, val in enumerate(args):
                    if i < len(params):
                        bound[params[i]] = val
                bound.update(kwargs)
                return BoundTask(
                    template_id=template_id,
                    fn=fn,
                    kwargs=bound,
                    env=self,
                )

            make_task_any._template_id = template_id
            make_task_any._fn = fn
            return make_task_any

        return decorator

    # ── @env.initialize / @env.shutdown ─────────────────────────────

    def initialize(self, fn: Callable) -> Callable:
        self.init_hooks.append(fn)
        return fn

    def shutdown(self, fn: Callable) -> Callable:
        self.shutdown_hooks.append(fn)
        return fn

    # ── capabilities ─────────────────────────────────────────────────

    def workspace(self, root: Path | str) -> None:
        """Declare that the agent gets a shell workspace rooted at `root`."""
        self._workspace_root = Path(root)
        self.capabilities.append(Capability.shell(name="shell"))

    def add_capability(self, cap: Any) -> None:
        """Add a capability to the environment.

        Accepts both TraceTensor Capability objects and HUD Capability objects
        (identified by duck-typing: any object with .name, .protocol, .url).
        """
        if isinstance(cap, Capability):
            self.capabilities.append(cap)
            return
        # HUD Capability duck-type: convert to ours
        try:
            tc = Capability(
                name=cap.name,
                protocol=cap.protocol,
                url=getattr(cap, "url", "") or "",
                params=dict(getattr(cap, "params", {}) or {}),
            )
            self.capabilities.append(tc)
        except Exception as exc:
            log.warning("add_capability_failed", extra={"error": str(exc)})

    async def run_init_hooks(self) -> None:
        for hook in self.init_hooks:
            result = hook()
            if asyncio.iscoroutine(result):
                await result

    async def run_shutdown_hooks(self) -> None:
        for hook in reversed(self.shutdown_hooks):
            result = hook()
            if asyncio.iscoroutine(result):
                await result


# ── Stub graders ─────────────────────────────────────────────────────

@dataclass
class EvaluationResult:
    score: float
    reason: str = ""

    def __float__(self) -> float:
        return float(self.score)


def combine(*results: Any, weights: list[float] | None = None) -> float:
    """Weighted combination of grader results → single 0-1 score."""
    vals: list[float] = []
    for r in results:
        if isinstance(r, EvaluationResult):
            vals.append(r.score)
        else:
            vals.append(float(r))
    if not vals:
        return 0.0
    if weights is None:
        return sum(vals) / len(vals)
    total_w = sum(weights[:len(vals)])
    if total_w == 0:
        return 0.0
    return sum(v * w for v, w in zip(vals, weights)) / total_w


# Stub BashGrader — grading via shell commands will use TraceTensor's
# own verifier; this stub keeps env.py imports from crashing.
class BashGrader:
    @classmethod
    async def grade(cls, *, weight: float = 1.0, command: str,
                    cwd: str | None = None) -> EvaluationResult:
        log.warning(
            "bash_grader_stub_called",
            extra={"command": command[:100]},
        )
        return EvaluationResult(score=0.0, reason="BashGrader stub: use TraceTensor verifier")


class LLMJudgeGrader:
    @classmethod
    async def grade(cls, *, weight: float = 1.0, rubric: str,
                    output: str, model: str | None = None) -> EvaluationResult:
        from app.services import llm_judge
        jr = llm_judge.judge(rubric, output, model, pass_threshold=0.5)
        return EvaluationResult(score=jr.reward, reason=jr.log)


# ── Stub settings ────────────────────────────────────────────────────

class _Settings:
    api_key: str = ""

settings = _Settings()


# ── Stub Workspace ───────────────────────────────────────────────────

class Workspace:
    """Stub for hud.environment.Workspace — env.py imports resolve cleanly."""
    def __init__(self, root: Path | str, *args: Any, **kwargs: Any) -> None:
        self.root = Path(root)

    def capability(self) -> Capability:
        return Capability.shell(name="shell")

    async def __aenter__(self) -> "Workspace":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass


# ── Module factory — inject as sys.modules entries ───────────────────

def _make_stub_module(name: str, attrs: dict) -> Any:
    import types
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def inject_hud_stubs() -> None:
    """Register stub modules so `from hud import Environment` works in env.py files."""
    import sys

    env_attrs = {
        "Environment": TensorEnvironment,
        "Workspace": Workspace,
    }
    grader_attrs = {
        "EvaluationResult": EvaluationResult,
        "combine": combine,
        "BashGrader": BashGrader,
        "LLMJudgeGrader": LLMJudgeGrader,
    }
    from app.capabilities.base import Capability as _Cap
    cap_attrs = {
        "Capability": _Cap,
    }

    hud_mod = _make_stub_module("hud", {
        "Environment": TensorEnvironment,
        "Workspace": Workspace,
        "Capability": _Cap,
    })
    sys.modules.setdefault("hud", hud_mod)
    sys.modules["hud.environment"] = _make_stub_module("hud.environment", env_attrs)
    sys.modules["hud.graders"] = _make_stub_module("hud.graders", grader_attrs)
    sys.modules["hud.capabilities"] = _make_stub_module("hud.capabilities", cap_attrs)
    sys.modules["hud.settings"] = _make_stub_module("hud.settings", {"settings": settings})
    # hud.environment.env is imported by some env.py files
    sys.modules["hud.environment.env"] = sys.modules["hud.environment"]
