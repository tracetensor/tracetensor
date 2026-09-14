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
    is_transient_llm_error,
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

    def _call_llm_with_retry(self, history: str, deadline: Optional[float]) -> "llm.LLMCallResult":
        """Call the LLM, retrying transient provider errors (overload, rate
        limit, 5xx, dropped connection) with exponential backoff instead of
        ending the whole trial on the first one. Non-transient errors
        (bad key, malformed request, ProviderError) are NOT retried — they
        fail the same way every time, so retrying just wastes the budget.

        Bounded on two independent axes so this can never itself cause a
        session-timeout-shaped failure: settings.LLM_AGENT_TRANSIENT_RETRY_MAX
        attempts, AND never sleeps past the session `deadline`.
        """
        max_attempts = max(1, settings.LLM_AGENT_TRANSIENT_RETRY_MAX)
        backoff = settings.LLM_AGENT_TRANSIENT_RETRY_BACKOFF_SEC
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                return self._llm_fn(self.provider, self.model, SYSTEM_PROMPT, history)
            except llm.ProviderError:
                raise  # never retryable — missing key/package, fails identically every time
            except Exception as e:
                last_exc = e
                if not is_transient_llm_error(e):
                    raise
                if attempt == max_attempts - 1:
                    break
                sleep_for = backoff * (2 ** attempt)
                if deadline is not None:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    sleep_for = min(sleep_for, remaining)
                log.warning(
                    "agent_llm_transient_retry",
                    extra={
                        "provider": self.provider,
                        "model": self.model,
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "error_type": type(e).__name__,
                        "sleep_s": round(sleep_for, 1),
                    },
                )
                time.sleep(sleep_for)
        if last_exc is None:
            raise RuntimeError("retry loop exhausted with no exception recorded")
        raise last_exc

    def run(
        self,
        instruction: str,
        env: BaseEnvironment,
        timeout: Optional[float] = None,
        on_event: EventHook = None,
        setup_timeout: Optional[float] = None,  # unused — no INSTALL step
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
        termination_reason: Optional[str] = None

        for _step_n in range(self.max_steps):
            if deadline and time.time() >= deadline:
                session_error = f"Agent session timed out after {timeout}s."
                termination_reason = "timeout"
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
                call = self._call_llm_with_retry(history, deadline)
            except llm.ProviderError as e:
                return AgentResult(
                    steps,
                    error=str(e),
                    llm_calls=llm_calls,
                    prompt_version=PROMPT_VERSION,
                    guardrail_flags=guardrail_flags,
                )
            except Exception as e:  # SDK / network / API error, retries exhausted
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
                    # First 2000 chars of the agent's actual reply — enough to
                    # classify why a trial ended (said DONE, emitted malformed
                    # XML, explored endlessly) without ballooning result.json.
                    "agent_text": (call.text or "")[:2000],
                    # True when model generated thinking blocks but no text command.
                    "thinking_only": getattr(call, "thinking_only", False),
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

            # Emit agent_text so live watchers can see what the model said.
            if on_event and text:
                on_event({
                    "type": "agent_text",
                    "phase": "agent",
                    "text": text[:500],
                    "output_tokens": call.output_tokens,
                })

            if not text.strip():
                # Empty text block. Two sub-cases:
                # 1. thinking_only=True — model spent tokens thinking but emitted no
                #    command. Retry with a nudge rather than silently stopping; this
                #    is NOT a DONE signal (the model didn't say DONE, it just got
                #    stuck). Real case: Opus 5 on PR 751 hit this every trial.
                # 2. Truly empty (no thinking either) — model returned nothing at all.
                #    Break with empty_response; no retry since there's nothing to nudge.
                if getattr(call, "thinking_only", False):
                    log.warning(
                        "agent_thinking_only_no_command",
                        extra={"provider": self.provider, "model": self.model, "output_tokens": call.output_tokens},
                    )
                    history += "\n\n(Your previous response had no bash command. Output ONE raw bash command now, or DONE.)"
                    continue
                termination_reason = "empty_response"
                break

            if text.upper().startswith("DONE"):
                termination_reason = "done_explicit"
                if not _agent_exec_steps(steps):
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
            # Cap this ONE command at settings.LLM_AGENT_COMMAND_TIMEOUT_SEC even
            # if more session time remains — otherwise a single hung/pathological
            # command (e.g. an unscoped `grep -r /`) can silently burn the whole
            # remaining budget in one step, leaving no turns for real work. The
            # overall session deadline above is unaffected: a command that hits
            # this cap just fails that one step (exit 124, "TIMEOUT after Ns" in
            # stderr) and the loop continues to the next command as normal.
            command_timeout = remaining
            if command_timeout is None or command_timeout > settings.LLM_AGENT_COMMAND_TIMEOUT_SEC:
                command_timeout = settings.LLM_AGENT_COMMAND_TIMEOUT_SEC
            result = env.exec(command, phase="agent", timeout=command_timeout)
            steps.append(result)
            if on_event:
                on_event(
                    {
                        "type": "step",
                        "phase": "agent",
                        "command": command,
                        "status": PhaseStatus.DONE.value,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout[-10000:],
                        "stderr": result.stderr[-2000:],
                    }
                )
            history += (
                f"\n\n$ {command}\n"
                f"[exit {result.exit_code}]\n{result.stdout[-10000:]}\n{result.stderr[-2000:]}\n"
                f"Return the next bash command, or DONE."
            )

        if session_error is None and llm_calls and not _agent_exec_steps(steps):
            session_error = _no_agent_work_error(llm_calls, last_text)

        # Stamp termination_reason onto the final llm_call so the downstream
        # reader can classify HOW the trial ended without re-parsing text.
        # Reasons: done_explicit | empty_response | timeout | step_budget_exhausted
        if termination_reason is None:
            termination_reason = "step_budget_exhausted"
        if llm_calls:
            llm_calls[-1] = {**llm_calls[-1], "termination_reason": termination_reason}

        return AgentResult(
            steps,
            error=session_error,
            llm_calls=llm_calls,
            prompt_version=PROMPT_VERSION,
            guardrail_flags=guardrail_flags,
        )
