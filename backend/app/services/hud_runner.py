"""
HUD-style task runner.

Executes a BoundTask (from the HUD adapter) through TraceTensor's infrastructure:

  1. Get the prompt from the async generator's first yield.
  2. Run the agent (LLM single-turn for text tasks, bash loop for workspace tasks).
  3. Feed the agent's answer into the generator's second yield → reward.

Two execution modes based on declared capabilities:
  - text mode: agent gets a single-turn LLM call, no Docker needed.
    Used for tasks like `blank` where grading is inline Python.
  - workspace mode: agent runs in a Docker container with bash access.
    Used when env.workspace() is declared.

Returns TrialOutcome so the result is identical to the existing trial runner's
output shape — callers and the API surface don't need to change.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Optional

from app.core.logging import get_logger
from app.models.enums import TrialStatus
from app.services.hud_compat import BoundTask, TensorEnvironment

log = get_logger("tracetensor.hud_runner")

EventHook = Optional[Callable[[dict], None]]


@dataclass
class HudTrialOutcome:
    """Same shape as TrialOutcome so callers are interchangeable."""
    status: str
    reward: float | None = None
    passed: bool | None = None
    trajectory: dict = field(default_factory=dict)
    verifier_log: str | None = None
    reward_payload: dict | None = None
    duration_s: float | None = None
    error: str | None = None
    warnings: list = field(default_factory=list)


def _has_workspace(env: TensorEnvironment) -> bool:
    return any(c.protocol == "shell/exec" for c in env.capabilities)


_TEXT_AGENT_SYSTEM = (
    "You are a precise task-completion agent. "
    "Read the task carefully and respond with ONLY the answer — "
    "no explanation, no punctuation, no units, no sentence. "
    "If the answer is a number, output the number alone. "
    "If the answer is text, output the text alone."
)


async def _run_text_agent(
    prompt: str,
    provider: str,
    model: str | None,
    timeout: float,
    on_event: EventHook,
) -> str:
    """Single-turn LLM call for text-only HUD tasks (no Docker)."""
    from app.services import llm

    provider = llm.canonical_provider(provider)
    resolved_model = model or llm.default_model(provider)
    if resolved_model is None:
        raise ValueError(f"No model for provider: {provider}")

    if on_event:
        on_event({"type": "thinking", "phase": "agent",
                  "provider": provider, "model": resolved_model})

    loop = asyncio.get_event_loop()
    call = await loop.run_in_executor(
        None,
        lambda: llm.call_llm(provider, resolved_model, _TEXT_AGENT_SYSTEM, prompt),
    )

    answer = (call.text or "").strip()
    if on_event:
        on_event({"type": "agent_text", "phase": "agent",
                  "text": answer[:500], "output_tokens": call.output_tokens})

    return answer


async def _run_workspace_agent(
    prompt: str,
    env_obj: TensorEnvironment,
    provider: str,
    model: str | None,
    task_dir: Any,
    timeout: float,
    on_event: EventHook,
) -> str:
    """
    Run the agent in a Docker workspace for tasks that need shell access.
    Wraps the synchronous LLMAgent in an executor so the async generator loop
    can await it.
    """
    from pathlib import Path
    from app.services.environment import make_environment
    from app.services.agents import make_agent

    task_dir = Path(task_dir)
    backend = "docker"

    # Build environment from Dockerfile.hud if present, else use python:3.11-slim
    has_dockerfile = (task_dir / "Dockerfile.hud").exists()
    docker_image = None if has_dockerfile else "python:3.11-slim"
    dockerfile_dir = "." if has_dockerfile else None

    env_kwargs: dict = {
        "network_mode": "public",
        "workdir": str(env_obj._workspace_root or "/workspace"),
    }
    if docker_image:
        env_kwargs["docker_image"] = docker_image
    if has_dockerfile:
        env_kwargs["dockerfile_dir"] = "."

    tt_env = make_environment(backend, task_dir, **env_kwargs)

    loop = asyncio.get_event_loop()

    def _run_sync() -> str:
        tt_env.setup()
        try:
            agent = make_agent(provider, model=model)
            result = agent.run(
                instruction=prompt,
                env=tt_env,
                timeout=timeout,
                on_event=on_event,
            )
            # Collect agent's final answer: last stdout or synthesized from steps
            steps = result.steps or []
            stdout_parts = [s.stdout for s in steps if s.stdout]
            return "\n".join(stdout_parts).strip() if stdout_parts else ""
        finally:
            try:
                tt_env.teardown()
            except Exception:
                pass

    return await loop.run_in_executor(None, _run_sync)


async def _grade_with_generator(
    gen: AsyncGenerator,
    answer: str,
) -> float:
    """Feed the answer to the generator's second yield and return the score."""
    try:
        raw_score = await gen.asend(answer)
    except StopAsyncIteration:
        return 0.0

    if isinstance(raw_score, (int, float)):
        return float(raw_score)
    if hasattr(raw_score, "score"):
        return float(raw_score.score)
    if isinstance(raw_score, dict) and "score" in raw_score:
        return float(raw_score["score"])
    try:
        return float(raw_score)
    except (TypeError, ValueError):
        log.warning("hud_score_unparseable", extra={"raw": str(raw_score)[:100]})
        return 0.0


async def run_hud_task(
    bound_task: BoundTask,
    provider: str = "openai",
    model: str | None = None,
    timeout: float = 120.0,
    pass_threshold: float = 1.0,
    task_dir: Any = None,
    on_event: EventHook = None,
) -> HudTrialOutcome:
    """Run one BoundTask end-to-end and return a HudTrialOutcome.

    provider: LLM provider name ("openai", "anthropic", "openrouter")
    model:    specific model id (None → provider default)
    timeout:  agent timeout in seconds
    """
    start = time.time()
    env_obj = bound_task.env
    warnings: list[str] = []

    log.info(
        "hud_trial_start",
        extra={
            "template": bound_task.template_id,
            "kwargs": str(bound_task.kwargs)[:200],
            "provider": provider,
            "model": model,
        },
    )

    if on_event:
        on_event({"type": "phase", "phase": "setup", "status": "running"})

    # Run init hooks (env.initialize)
    try:
        await env_obj.run_init_hooks()
    except Exception as exc:
        return HudTrialOutcome(
            status=TrialStatus.ERROR.value,
            error=f"env.initialize failed: {exc}",
            duration_s=time.time() - start,
        )

    if on_event:
        on_event({"type": "phase", "phase": "agent", "status": "running"})

    try:
        gen = bound_task.fn(**bound_task.kwargs)

        # First yield: get the prompt
        prompt = await gen.asend(None)
        if not isinstance(prompt, str):
            prompt = str(prompt)

        log.info("hud_prompt", extra={"prompt": prompt[:200]})

        # Run the agent
        workspace = _has_workspace(env_obj)
        if workspace and task_dir is not None:
            answer = await _run_workspace_agent(
                prompt, env_obj, provider, model, task_dir, timeout, on_event
            )
        else:
            answer = await _run_text_agent(
                prompt, provider, model, timeout, on_event
            )

        log.info("hud_agent_answer", extra={"answer": answer[:200]})

        if on_event:
            on_event({"type": "phase", "phase": "verify", "status": "running"})

        # Second yield: get the score
        reward = await _grade_with_generator(gen, answer)

    except Exception as exc:
        log.exception("hud_trial_error", extra={"error": str(exc)})
        return HudTrialOutcome(
            status=TrialStatus.ERROR.value,
            error=str(exc),
            duration_s=time.time() - start,
            warnings=warnings,
        )
    finally:
        # Always run shutdown hooks
        try:
            await env_obj.run_shutdown_hooks()
        except Exception as exc:
            log.warning("hud_shutdown_hook_error", extra={"error": str(exc)})

    passed = reward >= pass_threshold
    duration = time.time() - start

    log.info(
        "hud_trial_done",
        extra={
            "template": bound_task.template_id,
            "reward": reward,
            "passed": passed,
            "duration_s": round(duration, 2),
        },
    )
    if on_event:
        on_event({"type": "phase", "phase": "done", "status": "done",
                  "reward": reward, "passed": passed})

    return HudTrialOutcome(
        status=TrialStatus.COMPLETED.value,
        reward=reward,
        passed=passed,
        trajectory={"prompt": prompt, "answer": answer},  # type: ignore[possibly-undefined]
        verifier_log=f"reward={reward:.3f}",
        duration_s=duration,
        warnings=warnings,
    )


def run_hud_task_sync(
    bound_task: BoundTask,
    provider: str = "openai",
    model: str | None = None,
    timeout: float = 120.0,
    pass_threshold: float = 1.0,
    task_dir: Any = None,
    on_event: EventHook = None,
) -> HudTrialOutcome:
    """Synchronous wrapper around run_hud_task for use in CLI and tests."""
    return asyncio.run(run_hud_task(
        bound_task,
        provider=provider,
        model=model,
        timeout=timeout,
        pass_threshold=pass_threshold,
        task_dir=task_dir,
        on_event=on_event,
    ))
