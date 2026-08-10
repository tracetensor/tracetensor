"""Mini-SWE-Agent — installed via uv, driven by litellm."""

from __future__ import annotations

import shlex
from typing import List, Optional

from app.services.agents.base import (
    BaseInstalledAgent,
    litellm_api_key,
    normalize_litellm_model,
)
from app.services.cost import normalize_cost
from app.services.environment import BaseEnvironment, ExecResult


def _llm_calls_from_traj(raw: object, provider: str, model: str) -> List[dict]:
    """Roll an installed agent's own trajectory up into our llm_calls records.

    Mini-SWE writes aggregate spend to `info.model_stats` (instance_cost = total
    USD, api_calls = number of API calls) but no per-call token breakdown. We
    emit ONE aggregate record so the trial's cost summary reflects the real spend
    the agent made from inside the container — otherwise an installed agent (the
    one that actually spends money on the host's key) would report cost 0. Tokens
    stay None (unknown), which _summarize_llm_usage treats as 0, not as free.
    """
    if not isinstance(raw, dict):
        return []
    info = raw.get("info")
    stats = (info or {}).get("model_stats") if isinstance(info, dict) else None
    if not isinstance(stats, dict):
        return []
    cost = stats.get("instance_cost")
    api_calls = stats.get("api_calls")
    if cost is None and not api_calls:
        return []
    return [
        {
            "provider": provider,
            "model": model,
            "input_tokens": None,
            "output_tokens": None,
            "latency_ms": None,
            "cost_usd": normalize_cost(cost),
            # The agent's real per-run API-call count (our record is one aggregate
            # row, so len(llm_calls) alone would undercount it — keep the truth).
            "api_calls": api_calls,
        }
    ]


class MiniSweAgent(BaseInstalledAgent):
    """Mini-SWE-Agent — a real, capable coding agent (>70% on SWE-bench),
    installed via pip and run headless with `mini`. The model is a LiteLLM id,
    e.g. "anthropic/claude-haiku-4-5"; its native trajectory (with aggregate
    cost) is written to a file we then read."""

    PROMPT_VERSION = "mini-swe-agent"
    INSTALL = "pip install --quiet --disable-pip-version-check mini-swe-agent"
    TRAJ_PATH = "/logs/agent/mini.traj.json"

    def __init__(self, model: Optional[str]):
        self.name = "mini-swe"
        # Default to Anthropic's small model if only a bare/omitted model given.
        self.model, self.provider = normalize_litellm_model(model, "anthropic/claude-haiku-4-5")

    def _secret_env(self) -> dict:
        """The model API key the agent needs, under its LiteLLM env-var name."""
        env_name, value = litellm_api_key(self.provider, self.name, self.model)
        return {env_name: value}

    def _extra_env(self, env: BaseEnvironment) -> dict:
        # MSWEA_CONFIGURED skips the first-run wizard; SILENT_STARTUP quiets it.
        return {"MSWEA_CONFIGURED": "true", "MSWEA_SILENT_STARTUP": "true"}

    def _run_command(self, instruction: str, env: BaseEnvironment) -> str:

        return (
            f"mini -y -m {shlex.quote(self.model)} -l {self.COST_LIMIT_USD} "
            f"-t {shlex.quote(instruction)} -o {self.TRAJ_PATH}"
        )

    def _masked_command(self) -> str:
        return f"mini -y -m {self.model} …"

    def _read_trajectory(self, env: BaseEnvironment, run: ExecResult) -> Optional[dict]:
        import json

        traj_text = env.read_file(self.TRAJ_PATH)
        if not traj_text:
            return None
        try:
            parsed = json.loads(traj_text)
        except (ValueError, TypeError):
            return {"raw": traj_text[:20000]}
        # The agent's own file — a non-dict top level is possible, and callers
        # index into this, so wrap rather than hand back a bare list.
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    def _llm_calls(self, raw: object) -> List[dict]:
        return _llm_calls_from_traj(raw, self.provider, self.model)
