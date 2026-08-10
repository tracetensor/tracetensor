"""Claude Code CLI — installed from claude.ai, run headless with --print."""

from __future__ import annotations

import json
import shlex
from typing import List, Optional

from app.core.config import settings
from app.services import llm
from app.services.agents.base import BaseInstalledAgent
from app.services.cost import normalize_cost
from app.services.environment import BaseEnvironment, ExecResult


def _extract_json_object(text: Optional[str]) -> Optional[dict]:
    """Parse the single JSON object an agent prints to stdout (Claude Code's
    `--output-format json`). Tolerates a leading notice/blank line by falling
    back to the last balanced {...} block; never raises — returns a size-capped
    {"raw": …} if nothing parses, so a malformed result still leaves a record."""

    if not text or not text.strip():
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else {"raw": stripped[:20000]}
    except (ValueError, TypeError):
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(stripped[start : end + 1])
            return obj if isinstance(obj, dict) else {"raw": stripped[:20000]}
        except (ValueError, TypeError):
            pass
    return {"raw": stripped[:20000]}


def _llm_calls_from_claude_result(raw: object, model: str) -> List[dict]:
    """Roll Claude Code's `--output-format json` result up into our llm_calls
    records. `total_cost_usd` is the cumulative run cost (top-level); `num_turns`
    is the agentic-turn count (Claude Code's closest analogue to API calls). We
    leave tokens None: the result's `usage` reflects only the last turn, not the
    run total, so reporting it would understate — cost is the reliable figure."""
    if not isinstance(raw, dict):
        return []
    cost = raw.get("total_cost_usd")
    if cost is None and isinstance(raw.get("cost"), dict):  # tolerate a nested shape
        cost = raw["cost"].get("total_cost_usd")
    turns = raw.get("num_turns")
    if cost is None and not turns:
        return []
    return [
        {
            "provider": "anthropic",
            "model": model,
            "input_tokens": None,
            "output_tokens": None,
            "latency_ms": None,
            "cost_usd": normalize_cost(cost),
            "api_calls": turns,
        }
    ]


class ClaudeCodeAgent(BaseInstalledAgent):
    """Claude Code — Anthropic's own coding agent, installed and run headless in
    print mode. Anthropic-only; `model` is a Claude alias ("haiku"/"sonnet"/
    "opus") or a full model id, defaulting to Haiku. Its result JSON (with
    cumulative cost) is printed to stdout, which we capture as the trajectory.
    """

    PROMPT_VERSION = "claude-code"
    # Prefer an already-baked `claude`; otherwise use the official native installer
    # (no Node needed — the binary is native) and symlink it onto PATH. We used to
    # install via `apt-get nodejs npm`, but Debian's pool 403s on node-isexe in some
    # networks; the native script sidesteps Node entirely.
    INSTALL = (
        "command -v claude >/dev/null 2>&1 || { "
        "(command -v curl >/dev/null 2>&1 || "
        "{ apt-get update -qq && apt-get install -y -qq curl; }); "
        "curl --proto '=https' --tlsv1.2 -fsSL https://claude.ai/install.sh | bash; "
        '[ -x "$HOME/.local/bin/claude" ] && '
        'ln -sf "$HOME/.local/bin/claude" /usr/local/bin/claude; '
        "true; }"
    )
    MAX_TURNS = settings.AGENT_MAX_TURNS

    def __init__(self, model: Optional[str]):
        self.name = "claude-code"
        self.provider = "anthropic"
        m = model or "haiku"
        # Accept a litellm-style "anthropic/…" id (as mini-swe uses) and reduce it
        # to what Claude Code's --model wants: a bare alias or model id.
        if m.startswith("anthropic/"):
            m = m.split("/", 1)[1]
        self.model = m

    def _secret_env(self) -> dict:
        from app.core.config import settings

        if not settings.ANTHROPIC_API_KEY:
            raise llm.ProviderError("claude-code needs ANTHROPIC_API_KEY configured.")
        return {"ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY}

    def _extra_env(self, env: BaseEnvironment) -> dict:
        # IS_SANDBOX=1 lets --dangerously-skip-permissions run as root inside our
        # isolated container; DISABLE_AUTOUPDATER stops a mid-run background update.
        return {"IS_SANDBOX": "1", "DISABLE_AUTOUPDATER": "1"}

    def _run_command(self, instruction: str, env: BaseEnvironment) -> str:

        # --bare = reproducible (skips local config/hooks/OAuth, auth strictly from
        # ANTHROPIC_API_KEY); --max-turns/--max-budget-usd bound spend.
        return (
            f"claude --bare -p {shlex.quote(instruction)} "
            f"--model {shlex.quote(self.model)} --output-format json "
            f"--dangerously-skip-permissions "
            f"--max-turns {self.MAX_TURNS} --max-budget-usd {self.COST_LIMIT_USD}"
        )

    def _masked_command(self) -> str:
        return f"claude --bare -p … --model {self.model} --output-format json"

    def _read_trajectory(self, env: BaseEnvironment, run: ExecResult) -> Optional[dict]:
        return _extract_json_object(run.stdout)

    def _llm_calls(self, raw: object) -> List[dict]:
        return _llm_calls_from_claude_result(raw, self.model)
