"""
Agents — who attempts the task inside the Examination Room.

This package is the whole public surface. Everything outside it should import
from here (`from app.services.agents import make_agent`) rather than reaching
into a specific agent's module.

  base.py       the BaseAgent contract, AgentResult, BaseInstalledAgent
  registry.py   one AgentSpec per agent — the only file you edit to add one
  <agent>.py    one module per agent

Two entry points, deliberately separate:

  resolve_agent()  validates a *request* up front — is this a real agent, and is
                   its key configured? Returns the (label, model) to persist.
                   Called by the API before a job is created, so a misconfigured
                   run fails immediately with a 400 instead of burning a
                   container per trial to discover the same thing.
  make_agent()     builds the agent from that stored label. Called per trial by
                   the trial runner, long after the request is gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.services import llm
from app.services.agents.base import (
    MAX_HISTORY_CHARS,
    MAX_INSTRUCTION_CHARS,
    AgentResult,
    BaseAgent,
    BaseInstalledAgent,
    EventHook,
)
from app.services.agents.registry import (
    AGENTS,
    AgentSpec,
    agent_status,
    agent_status_catalog,
    lookup,
)

__all__ = [
    "AGENTS",
    "AgentConfigError",
    "AgentResult",
    "AgentSpec",
    "BaseAgent",
    "BaseInstalledAgent",
    "EventHook",
    "MAX_HISTORY_CHARS",
    "MAX_INSTRUCTION_CHARS",
    "agent_status",
    "agent_status_catalog",
    "lookup",
    "make_agent",
    "resolve_agent",
]


class AgentConfigError(ValueError):
    """A requested agent can't run as configured — unknown name, or its model's
    provider key isn't set. Raised by resolve_agent so the API gates can turn it
    into a 400 (and the CLI into a clean exit) without a stack trace."""


def resolve_agent(name: str, model: Optional[str], settings: object) -> tuple[str, Optional[str]]:
    """Validate an agent request and return the (label, model) to persist on the
    job — `make_agent` later instantiates `label`.

    `settings` is the app Settings (keys + available_providers()). Fails fast (not
    per-trial) with AgentConfigError on an unknown agent or a missing key. Shared
    by the single-task and dataset gates so both agree on exactly what's runnable.

    Custom "pkg:Class" agents are resolvable only when ALLOW_CUSTOM_AGENTS is on:
    importing a caller-named class is arbitrary code execution in the server
    process, which is fine for a local CLI run and not fine for a networked
    instance. Off by default; see the setting's comment.
    """
    spec = lookup(name)
    if spec is not None:
        # Gate unsupported agents at resolve time so the API returns a clean 400
        # instead of dispatching a job that fails inside the container.
        if spec.status == "unsupported":
            raise AgentConfigError(f"Agent '{spec.name}' is not yet supported: {spec.note}")
        model = model or spec.default_model
        if spec.key_check is not None:
            missing = spec.key_check(model or "", settings)
            if missing:
                raise AgentConfigError(
                    f"{spec.name} with model '{model}' needs {missing}. "
                    "Set it in backend/.env and restart."
                )
        return spec.name, model

    provider = llm.canonical_provider(name)
    if provider in llm.PROVIDERS:
        if not settings.available_providers().get(provider):  # type: ignore[attr-defined]
            key = llm.PROVIDER_KEY_ENV[provider]
            raise AgentConfigError(
                f"Agent '{name}' needs {key}. Set it in backend/.env and restart."
            )
        return provider, model or llm.default_model(provider)

    if ":" in name:
        if not getattr(settings, "ALLOW_CUSTOM_AGENTS", False):
            raise AgentConfigError(
                f"Custom agent '{name}' is not enabled on this instance. "
                "Set ALLOW_CUSTOM_AGENTS=true to allow the API to import agent "
                "classes by path (it executes code in the server process — only "
                "do this where you control every caller)."
            )
        return name, model

    raise AgentConfigError(f"Unknown agent: {name}")


def make_agent(
    name: str,
    task_dir: Path,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
) -> BaseAgent:
    """Build an agent. `name` is one of:

    - a registered agent   oracle / mini-swe|mini / claude-code|cc / codex — see registry.AGENTS
    - a provider/alias     ("anthropic"/"openai"/…) → the built-in bash loop
    - "pkg.mod:Class"      a custom agent class (plugin-style), imported live

    Dispatch goes through the registry's `factory`, so a registered agent is
    always constructible — there is no separate branch here to forget.
    """
    spec = lookup(name)
    if spec is not None:
        return spec.factory(task_dir, model)
    if ":" in name:  # custom agent, e.g. "mypkg.agents:MyAgent"
        return _load_custom_agent(name, model)
    provider = llm.canonical_provider(name)
    if provider in llm.PROVIDERS:
        from app.services.agents.llm_agent import LLMAgent

        return LLMAgent(provider=provider, model=model, max_steps=max_steps)
    raise ValueError(f"Unknown agent: {name}")


def _load_custom_agent(spec: str, model: Optional[str]) -> BaseAgent:
    """Import `module.path:ClassName` and instantiate it (`-a path:Class` syntax)."""
    import importlib

    mod_path, _, cls_name = spec.partition(":")
    try:
        mod = importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"Could not load custom agent '{spec}': {e}") from e
    try:
        agent = cls(model=model)
    except TypeError:
        agent = cls()
    # The class came from a caller-supplied import path, so nothing so far has
    # checked it actually implements the agent contract. Without this, a wrong
    # class is accepted here and fails much later inside the trial, where the
    # error looks like a task problem rather than a configuration one.
    if not isinstance(agent, BaseAgent):
        raise ValueError(
            f"Custom agent '{spec}' is not a BaseAgent subclass "
            f"(got {type(agent).__name__}). Subclass app.services.agents.base.BaseAgent."
        )
    return agent
