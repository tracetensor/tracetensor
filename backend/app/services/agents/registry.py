"""
The agent registry — one entry per agent, and nothing else to edit.

This file is the answer to "how do I add an agent?". Write your module next to
this one, then add one `AgentSpec` below. That single entry supplies everything
the rest of the system asks about an agent: how to build it, what model it
defaults to, which key it needs, what to call it in the UI, and whether we're
willing to claim it works.

Status labels are honest and must match the README:
  verified    — passed a real in-container run here
  gated       — wired up, but awaiting a key/subscription to verify
  unsupported — not ready to advertise; rejected at resolve time with its reason

Unverified agents are not registered here — backlog lives in the private tracetensor-full repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.services.agents.base import (
    LITELLM_PROVIDER_KEY_ATTR,
    BaseAgent,
    normalize_litellm_model,
)


def litellm_key_check(model: str, settings: object) -> Optional[str]:
    """Missing-key env name for a litellm '<provider>/<model>' id, or None if the
    key is set (bare id → anthropic). An unknown provider isn't blocked here — the
    run surfaces a clear litellm error rather than us guessing its key var."""
    _resolved, prov = normalize_litellm_model(model, model)
    attr = LITELLM_PROVIDER_KEY_ATTR.get(prov)
    if attr is None:
        return None
    return None if getattr(settings, attr, None) else attr


def fixed_key_check(attr: str) -> Callable[[str, object], Optional[str]]:
    """A key check for an agent that always needs one specific settings key."""

    def check(model: str, settings: object) -> Optional[str]:
        return None if getattr(settings, attr, None) else attr

    return check


@dataclass(frozen=True)
class AgentSpec:
    """Everything the system needs to know about one agent."""

    name: str
    factory: Callable[[Path, Optional[str]], BaseAgent]
    label: str
    status: str
    note: str
    default_model: Optional[str] = None
    key_check: Optional[Callable[[str, object], Optional[str]]] = None
    aliases: tuple = ()
    installed: bool = True


def _oracle(task_dir: Path, model: Optional[str]) -> BaseAgent:
    from app.services.agents.oracle import OracleAgent

    return OracleAgent(task_dir)


def _mini_swe(task_dir: Path, model: Optional[str]) -> BaseAgent:
    from app.services.agents.mini_swe import MiniSweAgent

    return MiniSweAgent(model)


def _claude_code(task_dir: Path, model: Optional[str]) -> BaseAgent:
    from app.services.agents.claude_code import ClaudeCodeAgent

    return ClaudeCodeAgent(model)


def _codex(task_dir: Path, model: Optional[str]) -> BaseAgent:
    from app.services.agents.codex import CodexAgent

    return CodexAgent(model)


def _langgraph(task_dir: Path, model: Optional[str]) -> BaseAgent:
    from app.services.agents.langgraph_agent import LangGraphAgent

    # project=None → taken from settings.LANGGRAPH_PROJECT, since the registry
    # factory only receives the task and the model.
    return LangGraphAgent(None, model)


def langgraph_key_check(model: str, settings: object) -> Optional[str]:
    """LangGraph needs a project to run *and* a model key to run it with. Report
    the project first: without it there is no agent at all, and a key error would
    point at the wrong missing piece."""
    if not getattr(settings, "LANGGRAPH_PROJECT", None):
        return "LANGGRAPH_PROJECT"
    return litellm_key_check(model, settings)


AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec(
            name="oracle",
            factory=_oracle,
            label="Oracle (solution/)",
            status="verified",
            note="Runs solve.sh — free, no API key.",
            installed=False,
        ),
        AgentSpec(
            name="mini-swe",
            factory=_mini_swe,
            label="Mini-SWE-Agent",
            status="verified",
            note="Passed a real in-container run on fix-add.",
            default_model="anthropic/claude-haiku-4-5",
            key_check=litellm_key_check,
            aliases=("mini",),
        ),
        AgentSpec(
            name="claude-code",
            factory=_claude_code,
            label="Claude Code",
            status="verified",
            note="Passed a real in-container run on fix-add.",
            default_model="haiku",
            key_check=fixed_key_check("ANTHROPIC_API_KEY"),
            aliases=("cc",),
        ),
        AgentSpec(
            name="codex",
            factory=_codex,
            label="Codex CLI",
            status="verified",
            note="Passed a real in-container run on fix-add.",
            default_model="gpt-5-codex",
            key_check=fixed_key_check("OPENAI_API_KEY"),
        ),
        AgentSpec(
            name="langgraph",
            factory=_langgraph,
            label="LangGraph / Deep Agents",
            status="verified",
            note="Passed real in-sandbox runs on langgraph-report (docker + daytona).",
            default_model="anthropic/claude-haiku-4-5",
            key_check=langgraph_key_check,
            aliases=("lg", "deep-agent"),
        ),
    )
}

_BY_REQUEST_NAME: dict[str, AgentSpec] = {}
for _spec in AGENTS.values():
    for _key in (_spec.name, *_spec.aliases):
        if _key in _BY_REQUEST_NAME:
            raise RuntimeError(f"Duplicate agent name/alias in the registry: {_key!r}")
        _BY_REQUEST_NAME[_key] = _spec


def lookup(name: str) -> Optional[AgentSpec]:
    """The spec for a request name or alias, or None if it isn't a registered
    agent (it may still be a provider name or a custom `pkg:Class` spec)."""
    return _BY_REQUEST_NAME.get(name)


_STATUS_MARK = {"verified": "✅", "gated": "⏳", "unsupported": "❌"}


def agent_status(name: str) -> dict:
    """Status metadata for a canonical agent label (or empty dict)."""
    spec = AGENTS.get(name)
    if spec is None:
        return {}
    return {"status": spec.status, "label": spec.label, "note": spec.note}


def agent_status_catalog() -> list:
    """UI/CLI table of every registered agent with an honest status mark."""
    return [
        {
            "id": spec.name,
            "label": spec.label,
            "status": spec.status,
            "mark": _STATUS_MARK.get(spec.status, "?"),
            "note": spec.note,
        }
        for spec in AGENTS.values()
    ]
