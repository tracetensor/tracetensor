"""Codex CLI — OpenAI's coding agent, run headless via `codex exec`."""

from __future__ import annotations

import json
import shlex
from typing import List, Optional

from app.services import llm
from app.services.agents.base import BaseInstalledAgent
from app.services.environment import BaseEnvironment, ExecResult


def _parse_jsonl(text: Optional[str]) -> List[dict]:
    """Parse newline-delimited JSON (the event stream Codex/Copilot print to
    stdout). Skips blank/garbled lines rather than failing the whole capture."""

    events: List[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _tokens_from_turns(events: List[dict]) -> tuple:
    """Run-total tokens from the LAST `turn.completed`, or (None, None).

    The usage on a `turn.completed` is cumulative for that turn, not the cost of
    one API call — an observed run reported input_tokens=3,403,871 (of which
    3,333,960 cached) in a single event, far beyond any one request's context
    window. `codex exec` emits one such event per run, so the last one carries
    the run total; taking the last rather than summing is what keeps a
    multi-event stream from double-counting a cumulative figure.

    `input_tokens` is already the TOTAL here, with `cached_input_tokens` a
    subset of it — not a separate bucket to add. An observed run reported
    input_tokens=3,403,871 against cached=3,333,960 + cache_write=69,707
    (=3,403,667, the remaining 204 being uncached), so adding the parts would
    report ~2x the real figure. Anthropic's shape is the opposite — there the
    cache fields ARE separate and do get summed — which is why the two adapters
    deliberately differ. `output_tokens` likewise already includes reasoning.

    Dollars stay None — Codex reports no USD and cost.py never estimates one.
    """
    last = None
    for e in events:
        if isinstance(e, dict) and e.get("type") == "turn.completed":
            u = e.get("usage")
            if isinstance(u, dict):
                last = u
    if not last:
        return None, None
    return (last.get("input_tokens") or None), (last.get("output_tokens") or None)


def _llm_calls_from_codex_jsonl(text: Optional[str], model: str) -> List[dict]:
    """Codex `exec --json` prints JSONL events; the turn count is the reliable
    call signal and `turn.completed.usage` carries the run's tokens. It emits NO
    USD cost, so cost_usd stays None — see cost.py: a figure we computed here
    would be an estimate, and this platform never publishes one."""
    events = _parse_jsonl(text)
    turns = sum(1 for e in events if e.get("type") == "turn.completed")
    if not turns:
        return []
    inp, out = _tokens_from_turns(events)
    return [
        {
            "provider": "openai",
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "latency_ms": None,
            "cost_usd": None,
            "api_calls": turns,
        }
    ]


class CodexAgent(BaseInstalledAgent):
    """OpenAI Codex CLI — a native Rust binary installed via OpenAI's install
    script (no Node needed). Run headless with `codex exec`, sandbox bypassed
    (Codex's own Landlock/seccomp sandbox fails inside Docker; the container IS
    the isolation boundary). Auth via CODEX_API_KEY, sourced from OPENAI_API_KEY.
    Its `--json` JSONL stream is captured as the trajectory. Model ids move fast
    (a GPT-5.x family) — pass -m for a specific one; the default just resolves.
    """

    PROMPT_VERSION = "codex-cli"
    VERSION_COMMAND = "codex --version"
    INSTALL = (
        "command -v codex >/dev/null 2>&1 || { "
        "(command -v curl >/dev/null 2>&1 || "
        "{ apt-get update -qq && apt-get install -y -qq curl; }); "
        "CODEX_NON_INTERACTIVE=1 curl --proto '=https' --tlsv1.2 -fsSL "
        "https://chatgpt.com/codex/install.sh | sh; "
        '[ -x "$HOME/.local/bin/codex" ] && ln -sf "$HOME/.local/bin/codex" /usr/local/bin/codex; '
        "true; }"
    )

    def __init__(self, model: Optional[str]):
        self.name = "codex"
        self.provider = "openai"
        # Accept a litellm-style "openai/…" id and reduce it to a bare model.
        m = model or "gpt-5-codex"
        if m.startswith("openai/"):
            m = m.split("/", 1)[1]
        self.model = m

    def _secret_env(self) -> dict:
        from app.core.config import settings

        if not settings.OPENAI_API_KEY:
            raise llm.ProviderError("codex needs OPENAI_API_KEY configured.")
        # codex exec authenticates a single run from CODEX_API_KEY (documented),
        # avoiding any stale auth.json — sourced from our OPENAI_API_KEY.
        return {"CODEX_API_KEY": settings.OPENAI_API_KEY}

    def _run_command(self, instruction: str, env: BaseEnvironment) -> str:

        return (
            f"codex exec --dangerously-bypass-approvals-and-sandbox "
            f"-m {shlex.quote(self.model)} --json --skip-git-repo-check "
            f"{shlex.quote(instruction)}"
        )

    def _masked_command(self) -> str:
        return f"codex exec --json -m {self.model} …"

    def _read_trajectory(self, env: BaseEnvironment, run: ExecResult) -> Optional[dict]:
        events = _parse_jsonl(run.stdout)
        return {"format": "codex-jsonl", "events": events[:400]} if events else None

    def _llm_calls(self, raw: object) -> List[dict]:
        events = raw.get("events") if isinstance(raw, dict) else None
        if not events:
            return []
        turns = sum(1 for e in events if isinstance(e, dict) and e.get("type") == "turn.completed")
        if not turns:
            return []
        inp, out = _tokens_from_turns(events)
        return [
            {
                "provider": "openai",
                "model": self.model,
                "input_tokens": inp,
                "output_tokens": out,
                "latency_ms": None,
                "cost_usd": None,
                "api_calls": turns,
            }
        ]
