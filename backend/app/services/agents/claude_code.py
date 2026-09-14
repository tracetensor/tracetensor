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


#: Caps on what we keep per step. A tool result can be an entire file; the point
#: of the record is to show what the agent did, not to mirror the workspace.
_MAX_STEP_TEXT = 2000
_MAX_STEPS = 200


def _content_blocks(event: dict) -> list:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _summarize_tool_result(block: dict) -> str:
    """A tool_result's content is either a string or a list of typed blocks."""
    content = block.get("content")
    if isinstance(content, str):
        return content[:_MAX_STEP_TEXT]
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(parts)[:_MAX_STEP_TEXT]
    return str(content)[:_MAX_STEP_TEXT] if content is not None else ""


def _parse_stream_json(stdout: Optional[str]) -> Optional[dict]:
    """Turn Claude Code's `--output-format stream-json` output into a record.

    The stream is newline-delimited JSON: a system init event, then assistant
    events carrying text and tool_use blocks, user events carrying tool_result
    blocks, and a final result event with the run's cumulative cost and usage.

    Previously we asked for `--output-format json` and kept only that final
    object, so a claude-code trial's trajectory was two shell steps — install and
    run — and said nothing about what the agent actually did. Here each tool call
    becomes a step, and its result is attached to it, which is the same shape the
    LangGraph adapter records.

    Never raises: a malformed line is skipped, and output that isn't a stream at
    all falls back to the single-object parser so an unexpected format still
    leaves a usable record.
    """
    if not stdout or not stdout.strip():
        return None

    events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            events.append(parsed)

    if not events:
        return _extract_json_object(stdout)

    steps: list[dict] = []
    pending: dict[str, dict] = {}  # tool_use_id -> the step awaiting its result
    result_event: Optional[dict] = None

    for event in events:
        kind = event.get("type")
        if kind == "result":
            result_event = event
            continue
        if kind == "assistant":
            for block in _content_blocks(event):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    steps.append({"kind": "text", "text": block["text"][:_MAX_STEP_TEXT]})
                elif block.get("type") == "tool_use":
                    step = {
                        "kind": "tool_use",
                        "tool": block.get("name"),
                        "input": str(block.get("input"))[:_MAX_STEP_TEXT],
                    }
                    steps.append(step)
                    if block.get("id"):
                        pending[block["id"]] = step
        elif kind == "user":
            for block in _content_blocks(event):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                # Attach to the call it answers, so a step carries both halves.
                step = pending.pop(block.get("tool_use_id"), None)
                if step is not None:
                    step["output"] = _summarize_tool_result(block)
                    step["is_error"] = bool(block.get("is_error"))

    if result_event is None and not steps:
        # Parsed JSON, but nothing that belongs to a stream — a real stream
        # always carries at least one assistant turn or a result event. This is
        # the single-object shape `--output-format json` produced (and what a
        # trajectory recorded before the switch looks like); read it as one.
        return _extract_json_object(stdout)

    # `kind`, not `type`: this project reserves a "type" field for the SSE
    # event vocabulary, and a trajectory step is not one of those.
    record: dict = {
        "format": "stream-json",
        "steps": steps[:_MAX_STEPS],
        "step_count": len(steps),
        "tool_calls": [s["tool"] for s in steps if s.get("kind") == "tool_use"],
    }
    if result_event is not None:
        record["result"] = result_event
    return record


def _tokens_from_model_usage(raw: dict) -> tuple:
    """Run-total token counts from Claude Code's `modelUsage` block, or (None,
    None) if it isn't present.

    Two usage shapes come back in the result JSON and only one is run-scoped:

      * `usage`      — the LAST turn only. Reporting it understates a run.
      * `modelUsage` — per-model totals for the WHOLE run.

    `modelUsage[*].costUSD` sums to the top-level `total_cost_usd`, which is
    documented as the cumulative run cost — that's what identifies modelUsage as
    run-scoped rather than per-turn.

    Every input flavour (fresh, cache-write, cache-read) is summed into
    input_tokens: all three are input the model processed, they're just billed
    at different rates. Dollars still come from the vendor's own figure, so this
    split never has to be priced here.
    """
    usage = raw.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None, None
    inp = out = 0
    for entry in usage.values():
        if not isinstance(entry, dict):
            continue
        inp += (
            (entry.get("inputTokens") or 0)
            + (entry.get("cacheCreationInputTokens") or 0)
            + (entry.get("cacheReadInputTokens") or 0)
        )
        out += entry.get("outputTokens") or 0
    return (inp or None), (out or None)


def _llm_calls_from_claude_result(raw: object, model: str) -> List[dict]:
    """Roll Claude Code's `--output-format json` result up into our llm_calls
    records. `total_cost_usd` is the cumulative run cost (top-level); `num_turns`
    is the agentic-turn count (Claude Code's closest analogue to API calls);
    tokens come from `modelUsage` (see _tokens_from_model_usage for why that
    block and not `usage`).

    One record per run, not per model: _summarize_llm_usage sums `api_calls`
    across records, so splitting a single run into several would multiply the
    reported call count.
    """
    if not isinstance(raw, dict):
        return []
    cost = raw.get("total_cost_usd")
    if cost is None and isinstance(raw.get("cost"), dict):  # tolerate a nested shape
        cost = raw["cost"].get("total_cost_usd")
    turns = raw.get("num_turns")
    inp, out = _tokens_from_model_usage(raw)
    if cost is None and not turns and inp is None:
        return []
    return [
        {
            "provider": "anthropic",
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
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
    VERSION_COMMAND = "claude --version"

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
        # stream-json (which requires --verbose in print mode) emits every turn
        # as it happens, so the trajectory can show the agent's actual tool calls
        # instead of only the final summary object that `--output-format json`
        # returns.
        return (
            f"claude --bare -p {shlex.quote(instruction)} "
            f"--model {shlex.quote(self.model)} --output-format stream-json --verbose "
            f"--dangerously-skip-permissions "
            f"--max-turns {self.MAX_TURNS} --max-budget-usd {self.COST_LIMIT_USD}"
        )

    def _masked_command(self) -> str:
        return f"claude --bare -p … --model {self.model} --output-format stream-json"

    def _read_trajectory(self, env: BaseEnvironment, run: ExecResult) -> Optional[dict]:
        return _parse_stream_json(run.stdout)

    def _llm_calls(self, raw: object) -> List[dict]:
        # Cost and usage live on the stream's final result event; older
        # single-object output has them at the top level. Accept both so a
        # trajectory recorded either way still prices correctly.
        if isinstance(raw, dict) and isinstance(raw.get("result"), dict):
            raw = raw["result"]
        return _llm_calls_from_claude_result(raw, self.model)
