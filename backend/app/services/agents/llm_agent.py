"""
The built-in bash loop — our own agent, not a third-party CLI.

Asks the model for one bash command at a time, runs it in the container, feeds
the result back, and repeats until the model says DONE or the step budget runs
out. Provider-agnostic: anthropic / openai / openrouter all go through
app.services.llm.

This is the agent that runs when you pass a provider name ("anthropic") rather
than an installed-agent name ("claude-code").
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

from app.core.config import settings
from app.models.enums import PhaseStatus
from app.prompts.bash_agent import PROMPT_VERSION, SYSTEM_PROMPT
from app.services import guardrails, llm
from app.services.agents.base import (
    MAX_HISTORY_CHARS,
    MAX_INSTRUCTION_CHARS,
    AgentResult,
    BaseAgent,
    EventHook,
    _agent_exec_steps,
    _clean_command,
    _no_agent_work_error,
    log,
)
from app.services.cost import normalize_cost
from app.services.environment import BaseEnvironment, ExecResult


class LLMAgent(BaseAgent):
    """Provider-agnostic agentic bash loop."""

    def __init__(
        self,
        provider: str,
        model: Optional[str] = None,
        max_steps: Optional[int] = None,
        llm_fn: Optional[Callable[..., "llm.LLMCallResult"]] = None,
    ):
        """`llm_fn` is the seam that makes this class testable.

        It defaults to the real `llm.call_llm`, so production is unchanged. Pass
        a stand-in — a list of scripted replies, one that raises — and the retry
        path, DONE detection, guardrail handling, history trimming, and timeout
        behavior all become ordinary unit tests. Without it, every one of those
        needed `patch("app.services.llm.call_llm")`, which is global state and
        makes tests order-dependent.
        """
        provider = llm.canonical_provider(provider)
        if provider not in llm.PROVIDERS:
            raise ValueError(f"Unknown provider: {provider}")
        self.provider = provider
        self.name = provider
        resolved = model or llm.default_model(provider)
        # A real guard, not an assert: `python -O` strips asserts, and a None here
        # would flow into every downstream API call as a missing model id.
        if resolved is None:
            raise ValueError(f"No model given and no default model for provider: {provider}")
        self.model: str = resolved
        self.max_steps = max_steps if max_steps is not None else settings.LLM_AGENT_MAX_STEPS
        self._llm_fn = llm_fn or llm.call_llm

    def run(
        self,
        instruction: str,
        env: BaseEnvironment,
        timeout: Optional[float] = None,
        on_event: EventHook = None,
    ) -> AgentResult:
        # instruction.md is task-author content, not trusted input — cap it
        # before it ever reaches the prompt (cost + prompt-injection surface).
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            instruction = instruction[:MAX_INSTRUCTION_CHARS] + "\n… (truncated, too long)"
        history = f"TASK:\n{instruction}\n\nBegin. Return one bash command."
        steps: List[ExecResult] = []
        llm_calls: List[dict] = []  # token/latency/cost per call — see AgentResult
        guardrail_flags: List[dict] = []  # flagged commands — see app.services.guardrails
        # `timeout` is the TOTAL agent-session budget (standard semantics), not a
        # per-command limit. Each command is capped by the remaining budget.
        deadline = (time.time() + timeout) if timeout else None
        session_error: Optional[str] = None
        last_text = ""

        for _ in range(self.max_steps):
            if deadline and time.time() >= deadline:
                session_error = f"Agent session timed out after {timeout}s."
                break
            if len(history) > MAX_HISTORY_CHARS:
                # Trim from the front (oldest turns), keep the task statement —
                # unbounded conversation growth is a cost/DoS vector too.
                head, _, _ = history.partition("\n\nBegin.")
                tail = history[-MAX_HISTORY_CHARS:]
                history = f"{head[:2000]}\n\n… (earlier turns trimmed) …\n{tail}"
            if on_event:
                on_event(
                    {
                        "type": "thinking",
                        "phase": "agent",
                        "provider": self.provider,
                        "model": self.model,
                    }
                )
            try:
                call = self._llm_fn(self.provider, self.model, SYSTEM_PROMPT, history)
            except llm.ProviderError as e:
                return AgentResult(
                    steps,
                    error=str(e),
                    llm_calls=llm_calls,
                    prompt_version=PROMPT_VERSION,
                    guardrail_flags=guardrail_flags,
                )
            except Exception as e:  # SDK / network / API error
                return AgentResult(
                    steps,
                    error=f"Model call failed ({self.provider}): {e}",
                    llm_calls=llm_calls,
                    prompt_version=PROMPT_VERSION,
                    guardrail_flags=guardrail_flags,
                )

            llm_calls.append(
                {
                    "provider": call.provider,
                    "model": call.model,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "latency_ms": round(call.latency_ms, 1)
                    if call.latency_ms is not None
                    else None,
                    "cost_usd": normalize_cost(call.cost_usd),
                }
            )
            log.info(
                "agent_llm_call",
                extra={
                    "provider": call.provider,
                    "model": call.model,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "latency_ms": call.latency_ms,
                },
            )
            text = call.text
            last_text = text

            if text.upper().startswith("DONE") or not text.strip():
                if text.upper().startswith("DONE") and not _agent_exec_steps(steps):
                    session_error = _no_agent_work_error(llm_calls, text)
                break

            command = _clean_command(text)
            if not command:
                log.warning(
                    "agent_no_command",
                    extra={
                        "provider": self.provider,
                        "model": self.model,
                        "output_tokens": call.output_tokens,
                        "response_preview": text[:200],
                    },
                )
                history += "\n\n(No runnable command detected. Reply with ONE raw bash command.)"
                continue

            remaining = (deadline - time.time()) if deadline else None
            if remaining is not None and remaining <= 0:
                session_error = f"Agent session timed out after {timeout}s."
                break

            # Defense-in-depth: flag (don't silently allow) commands shaped
            # like credential theft, sandbox escape, or destructive patterns.
            # The sandbox is the real defense; this makes risky behavior loud.
            flags = guardrails.scan_command(command)
            if flags:
                for f in flags:
                    entry = {"category": f.category, "message": f.message, "command": command[:300]}
                    guardrail_flags.append(entry)
                    log.warning(
                        "agent_guardrail_block",
                        extra={"category": f.category, "detail": f.message},
                    )
                    if on_event:
                        on_event({"type": "guardrail", "phase": "agent", **entry})
                if guardrails.GUARDRAIL_MODE == "block":
                    steps.append(
                        ExecResult(
                            command=command,
                            exit_code=126,
                            stdout="",
                            stderr=f"blocked by guardrail: {flags[0].message}",
                            duration_s=0.0,
                            phase="agent",
                        )
                    )
                    history += (
                        f"\n\n$ {command}\n[blocked by guardrail: {flags[0].message}]\n"
                        f"Return a different bash command, or DONE."
                    )
                    continue

            if on_event:
                on_event(
                    {
                        "type": "step",
                        "phase": "agent",
                        "command": command,
                        "status": PhaseStatus.RUNNING.value,
                    }
                )
            result = env.exec(command, phase="agent", timeout=remaining)
            steps.append(result)
            if on_event:
                on_event(
                    {
                        "type": "step",
                        "phase": "agent",
                        "command": command,
                        "status": PhaseStatus.DONE.value,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout[-2000:],
                        "stderr": result.stderr[-800:],
                    }
                )
            history += (
                f"\n\n$ {command}\n"
                f"[exit {result.exit_code}]\n{result.stdout[-1500:]}\n{result.stderr[-500:]}\n"
                f"Return the next bash command, or DONE."
            )

        if session_error is None and llm_calls and not _agent_exec_steps(steps):
            session_error = _no_agent_work_error(llm_calls, last_text)

        return AgentResult(
            steps,
            error=session_error,
            llm_calls=llm_calls,
            prompt_version=PROMPT_VERSION,
            guardrail_flags=guardrail_flags,
        )
