"""
Trial runner — orchestrates a single examination session.

One trial = build the room, let the agent work, run the health check, record
the trajectory + score. The runner is environment-agnostic (Docker or Local)
and agent-agnostic (oracle or claude), so the same loop powers keyless oracle
testing and real LLM runs.

Returned TrialOutcome is what the router persists to the `trials` table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from app.core.logging import get_logger
from app.models.enums import PhaseStatus, TrialStatus
from app.services.agents import make_agent
from app.services.cost import normalize_cost
from app.services.docker_platform import resolve_docker_platform
from app.services.environment import make_environment
from app.services.verifier import run_verifier

if TYPE_CHECKING:  # import-cycle-free typing for the factory seams
    from app.services.agents.base import BaseAgent
    from app.services.environment import BaseEnvironment

log = get_logger("tracetensor.trial")

# on_event(evt: dict) -> None. Optional live-progress hook. run_trial calls it at
# each phase boundary (setup / agent / verify / score) so the UI can stream a
# task-progress checklist. The agent forwards its own thinking/step events too.
EventHook = Optional[Callable[[dict], None]]

# LogRecord reserved keys — must not appear in logger `extra=`.
_LOG_EXTRA_SKIP = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def _phase(on_event: EventHook, phase: str, status: str, **extra: object) -> None:
    safe_extra = {k: v for k, v in extra.items() if k not in _LOG_EXTRA_SKIP}
    log.info("trial_phase", extra={"phase": phase, "status": status, **safe_extra})
    if on_event:
        on_event({"type": "phase", "phase": phase, "status": status, **extra})


def _warn(on_event: EventHook, sink: list, message: str) -> None:
    """Record a non-fatal warning on the trial and stream it live. Used to make
    silent-0.0 conditions (e.g. an isolated verifier with no artifacts to grade)
    visible instead of looking like a genuine agent failure."""
    sink.append(message)
    if on_event:
        on_event({"type": "warning", "phase": "verify", "message": message})


def _summarize_llm_usage(llm_calls: list) -> dict:
    """Roll up a trial's per-call token/latency/cost records into one summary
    so the UI/API doesn't need to sum the list itself. cost_total is None (not
    0) if any call's cost is unknown, so a partial estimate never masquerades
    as a complete one."""
    if not llm_calls:
        return {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "latency_ms": 0.0,
            "cost_usd": None,
        }
    costs = [c.get("cost_usd") for c in llm_calls]
    return {
        # One record per real call for the built-in loop; an installed agent
        # (mini-swe) reports its whole run as one record carrying the true
        # api_calls count — prefer that so the number isn't undercounted to 1.
        "calls": sum(c.get("api_calls") or 1 for c in llm_calls),
        "input_tokens": sum(c.get("input_tokens") or 0 for c in llm_calls),
        "output_tokens": sum(c.get("output_tokens") or 0 for c in llm_calls),
        "latency_ms": round(sum(c.get("latency_ms") or 0 for c in llm_calls), 1),
        "cost_usd": normalize_cost(
            round(sum(costs), 6) if all(c is not None for c in costs) else None
        ),
    }


def _run_llm_judge(
    env: BaseEnvironment,
    tests_dir: Path,
    rubric: str | None,
    rubric_file: str | None,
    judge_model: str | None,
    judge_output_path: str | None,
    agent_result: Any,
    pass_threshold: float,
    on_event: EventHook,
    warnings: list,
) -> Any:
    """Grade the agent's output with a model against a rubric → a VerifierResult
    (same shape as the script path, so the rest of run_trial is unchanged)."""
    from app.services import llm_judge
    from app.services.environment import ExecResult
    from app.services.verifier import VerifierResult

    rubric_text = rubric or ""
    if not rubric_text and rubric_file:  # rubric can live in a tests/ file
        p = (tests_dir / rubric_file).resolve()
        if p.is_relative_to(tests_dir.resolve()) and p.exists():
            rubric_text = p.read_text(errors="replace")
    # What to grade: a declared container file, else the agent's own stdout.
    output = env.read_file(judge_output_path) if judge_output_path else None
    if not output:
        output = "\n".join(s.stdout for s in agent_result.steps if s.stdout)

    jr = llm_judge.judge(rubric_text, output, judge_model, pass_threshold)
    if jr.error:
        _warn(on_event, warnings, f"llm-judge: {jr.error}")
    step = ExecResult(
        command=f"llm-judge ({jr.payload.get('judge_model', judge_model or 'default')})",
        exit_code=0 if jr.error is None else 1,
        stdout=jr.log[:2000],
        stderr=jr.error or "",
        duration_s=0.0,
        phase="verifier",
    )
    return VerifierResult(
        reward=jr.reward, passed=jr.passed, payload=jr.payload, log=jr.log, step=step
    )


@dataclass
class TrialOutcome:
    status: str  # a TrialStatus value — COMPLETED or ERROR when run_trial returns
    reward: float | None = None
    passed: bool | None = None
    trajectory: dict = field(default_factory=dict)
    verifier_log: str | None = None
    reward_payload: dict | None = None
    duration_s: float | None = None
    error: str | None = None
    warnings: list = field(default_factory=list)


def prebuild(task_dir: Path, backend: str = "docker") -> None:
    """Build the task's image(s) once, serially, before parallel trials fan out.

    With a stable per-task image tag, the first trial normally triggers the
    (slow) Docker build and every later trial hits the layer cache. Under
    parallelism that first build would happen N times at once — same tag, all
    cold. Warming it here once means the concurrent trials all hit the cache.

    Best-effort: any failure is swallowed so trials still build lazily and the
    real error surfaces per-trial (where it's recorded) instead of failing the
    whole job here.
    """
    if backend != "docker":
        return
    try:
        from app.services.task_parser import parse_task_toml

        cfg = parse_task_toml((task_dir / "task.toml").read_bytes(), task_dir)
        e = cfg.environment
        make_environment(
            backend,
            task_dir,
            docker_image=e.docker_image,
            build_timeout=e.build_timeout_sec,
            platform=resolve_docker_platform(e.platform),
        ).build()
        # The isolated verifier runs from tests/Dockerfile — warm that image too.
        if cfg.verifier.environment_mode == "separate":
            make_environment(
                backend,
                task_dir,
                dockerfile_dir="tests",
                build_timeout=e.build_timeout_sec,
            ).build()
    except Exception:
        pass


def _run_trial_once(
    task_dir: Path,
    instruction: str,
    agent_name: str = "oracle",
    model: str | None = None,
    backend: str = "docker",
    docker_image: str | None = None,
    agent_timeout: float = 120.0,
    verifier_timeout: float = 120.0,
    pass_threshold: float = 1.0,
    on_event: EventHook = None,
    environment_factory: Callable[..., "BaseEnvironment"] = make_environment,
    agent_factory: Callable[..., "BaseAgent"] = make_agent,
    platform_override: str | None = None,
) -> TrialOutcome:
    """Run one trial end to end: build the room, let the agent work, grade it.

    `environment_factory` and `agent_factory` are seams for tests. They default
    to the real factories, so nothing changes in production; passing stand-ins
    lets the phase sequencing, warning accumulation, artifact-transfer checks,
    and error surfacing be tested without Docker and without an API key. Before
    they existed, exercising any of that meant a real container — so in practice
    it went untested, which is how the silently-swallowed task.toml parse
    survived as long as it did.
    """
    start = time.time()
    steps: list = []
    warnings: list = []
    task_label = task_dir.name
    log.info(
        "trial_start",
        extra={"task": task_label, "agent": agent_name, "model": model, "backend": backend},
    )

    # The verifier runs as root so it can write the locked reward dir, unless the
    # task overrides it below. Everything else comes from task.toml.
    verifier_user = "0"

    # Parsing task.toml is FATAL, never best-effort. Every isolation guarantee the
    # trial makes — network_mode, resource caps, the agent's user, the verifier's
    # environment — is read from here. Swallowing a parse error would silently run
    # a task declared `no-network` with full network access, and the operator would
    # see a normal-looking result. Fail the trial instead.
    try:
        from app.services.task_parser import parse_task_toml

        cfg = parse_task_toml((task_dir / "task.toml").read_bytes(), task_dir)
    except Exception as exc:
        detail = f"Could not read task.toml — cannot establish the sandbox contract: {exc}"
        _phase(on_event, "setup", PhaseStatus.ERROR, detail=detail[:200])
        log.error(
            "trial_error",
            extra={"task": task_label, "phase": "config", "error": detail[:200]},
        )
        return TrialOutcome(
            status=TrialStatus.ERROR.value, error=detail, duration_s=time.time() - start
        )

    e = cfg.environment
    baseline_net = e.network_mode
    build_timeout = e.build_timeout_sec
    resolved_platform = resolve_docker_platform(e.platform, override=platform_override)
    env_cfg = dict(
        docker_image=e.docker_image,
        build_timeout=e.build_timeout_sec,
        network_mode=e.network_mode,
        allowed_hosts=e.allowed_hosts,
        cpus=e.cpus,
        memory_mb=e.memory_mb,
        storage_mb=e.storage_mb,
        workdir=e.workdir,
        user=cfg.agent.user,
        env=e.env,
        platform=resolved_platform,
    )
    agent_net = cfg.agent.network_mode
    verifier_net = cfg.verifier.network_mode
    separate_verifier = cfg.verifier.environment_mode == "separate"
    artifacts = list(cfg.artifacts)
    verifier_env = cfg.verifier.env
    verifier_type = cfg.verifier.type
    rubric = cfg.verifier.rubric
    rubric_file = cfg.verifier.rubric_file
    judge_model = cfg.verifier.judge_model
    judge_output_path = cfg.verifier.judge_output_path
    if cfg.verifier.user is not None:
        verifier_user = str(cfg.verifier.user)

    env = environment_factory(backend, task_dir, **env_cfg)

    # 0. Build the room — this is the slow phase (Docker image build + container).
    _phase(on_event, "setup", PhaseStatus.RUNNING, detail="building image")
    try:
        env.setup()
    except Exception as e:
        _phase(on_event, "setup", PhaseStatus.ERROR, detail=str(e)[:200])
        log.error(
            "trial_error",
            extra={"task": task_label, "phase": "setup", "error": str(e)[:200]},
        )
        return TrialOutcome(
            status=TrialStatus.ERROR.value,
            error=f"Room setup failed: {e}",
            duration_s=time.time() - start,
        )
    _phase(on_event, "setup", PhaseStatus.DONE)

    verifier_env_obj = None
    try:
        # 1. Agent works. Apply the agent-phase network override (or baseline).
        env.set_network(agent_net or baseline_net)
        _phase(on_event, "agent", PhaseStatus.RUNNING)
        agent = agent_factory(agent_name, task_dir, model=model)
        agent_result = agent.run(instruction, env, timeout=agent_timeout, on_event=on_event)
        steps.extend(s.to_step() for s in agent_result.steps)
        _phase(on_event, "agent", PhaseStatus.DONE, agent_error=agent_result.error)
        agent_steps = [s for s in agent_result.steps if getattr(s, "phase", None) == "agent"]
        if agent_result.error and not agent_steps:
            _warn(on_event, warnings, f"agent: {agent_result.error}")
        elif (
            getattr(agent_result, "llm_calls", None) and not agent_steps and not agent_result.error
        ):
            msg = "agent: LLM was called but no bash command was executed"
            _warn(on_event, warnings, msg)
            agent_result.error = msg
        # Guardrail flags (see app.services.guardrails) surface through the
        # same loud-not-silent warnings channel as the separate-verifier checks.
        for f in getattr(agent_result, "guardrail_flags", None) or []:
            _warn(on_event, warnings, f"guardrail: {f['message']} — `{f['command']}`")

        # 2. Verifier checks health (even if the agent errored — we still score).
        _phase(on_event, "verify", PhaseStatus.RUNNING)
        tests_dir = task_dir / "tests"
        if verifier_type == "llm-judge":
            # Grade the agent's output with a model against a rubric (no test.sh).
            verdict = _run_llm_judge(
                env,
                tests_dir,
                rubric,
                rubric_file,
                judge_model,
                judge_output_path,
                agent_result,
                pass_threshold,
                on_event,
                warnings,
            )
        elif separate_verifier:
            # Isolated grader: a FRESH container built from tests/Dockerfile that
            # the agent never touched. Copy the agent's declared artifacts across,
            # then run the baked-in test.sh. Tampering the agent's own test.sh
            # cannot affect this grader.
            verifier_env_obj = environment_factory(
                backend,
                task_dir,
                dockerfile_dir="tests",
                network_mode=(verifier_net or baseline_net),
                env=verifier_env,
                build_timeout=build_timeout,
            )
            verifier_env_obj.setup()
            # The isolated grader sees ONLY what we transfer. If nothing is
            # declared, or a declared artifact is missing from the agent
            # container, it silently reads nothing and scores 0.0 — which looks
            # identical to a genuine failure. Detect both and warn loudly.
            if not artifacts:
                _warn(
                    on_event,
                    warnings,
                    "Isolated verifier (environment_mode = separate) declared but no "
                    "`artifacts` to transfer — the grader only sees /logs/artifacts and "
                    "will likely score 0.0. Is `artifacts` a root-level key in task.toml "
                    "(not nested under a [table])?",
                )
            skipped = verifier_env_obj.transfer_from(env, ["/logs/artifacts"] + artifacts)
            for p in [p for p in skipped if p in artifacts]:
                _warn(
                    on_event,
                    warnings,
                    f"Declared artifact not found in the agent container: {p} — the "
                    "isolated grader won't see it.",
                )
            verdict = run_verifier(
                verifier_env_obj,
                tests_dir,
                timeout=verifier_timeout,
                pass_threshold=pass_threshold,
                verifier_user=verifier_user,
                copy_tests=False,
            )  # test.sh is baked into the image
        else:
            env.set_network(verifier_net or baseline_net)  # verifier-phase network
            verdict = run_verifier(
                env,
                tests_dir,
                timeout=verifier_timeout,
                pass_threshold=pass_threshold,
                verifier_user=verifier_user,
            )
        steps.append(verdict.step.to_step())
        if on_event:
            vs = verdict.step
            on_event(
                {
                    "type": "step",
                    "phase": "verifier",
                    "command": vs.command,
                    "status": PhaseStatus.DONE.value,
                    "exit_code": vs.exit_code,
                    "stdout": vs.stdout[-2000:],
                    "stderr": vs.stderr[-800:],
                }
            )
        _phase(on_event, "verify", PhaseStatus.DONE)

        # 3. Score (reward already parsed by the verifier).
        _phase(on_event, "score", PhaseStatus.DONE, reward=verdict.reward, passed=verdict.passed)

        llm_calls = list(getattr(agent_result, "llm_calls", None) or [])
        # An LLM-judge verifier spends too — fold its call into the trial's cost
        # instrumentation so the summary reflects the full spend, not just the agent.
        judge_call = (verdict.payload or {}).get("llm_call") if verdict.payload else None
        if judge_call:
            llm_calls.append(judge_call)
        trajectory = {
            "agent": agent_name,
            "model": model,
            "steps": steps,
            "agent_error": agent_result.error,
            "warnings": warnings,
            # LLM spend/latency instrumentation — see app.services.llm.LLMCallResult.
            # Empty for agents that don't call a model (e.g. oracle).
            "llm_calls": llm_calls,
            "llm_usage_summary": _summarize_llm_usage(llm_calls),
            "prompt_version": getattr(agent_result, "prompt_version", None),
            "guardrail_flags": getattr(agent_result, "guardrail_flags", None) or [],
            # An external agent's own trajectory (e.g. Mini-SWE's), if it produced
            # one — its full internal reasoning, preserved for inspection/export.
            "agent_native_trajectory": getattr(agent_result, "raw_trajectory", None),
        }

        duration = time.time() - start
        log.info(
            "trial_end",
            extra={
                "task": task_label,
                "agent": agent_name,
                "model": model,
                "passed": verdict.passed,
                "reward": verdict.reward,
                "duration_s": round(duration, 2),
                "agent_steps": len(agent_steps),
                "llm_calls": len(llm_calls),
            },
        )

        return TrialOutcome(
            status=TrialStatus.COMPLETED.value,
            reward=verdict.reward,
            passed=verdict.passed,
            trajectory=trajectory,
            verifier_log=verdict.log,
            reward_payload=verdict.payload,
            duration_s=duration,
            error=agent_result.error,  # surfaced but not fatal
            warnings=warnings,
        )
    except Exception as e:
        _phase(on_event, "score", PhaseStatus.ERROR, detail=str(e)[:200])
        log.error(
            "trial_error",
            extra={"task": task_label, "phase": "score", "error": str(e)[:200]},
        )
        return TrialOutcome(
            status=TrialStatus.ERROR.value,
            trajectory={"agent": agent_name, "steps": steps, "warnings": warnings},
            error=f"Trial failed: {e}",
            duration_s=time.time() - start,
            warnings=warnings,
        )
    finally:
        env.teardown()
        if verifier_env_obj is not None:
            verifier_env_obj.teardown()


def run_trial(
    task_dir: Path,
    instruction: str,
    agent_name: str = "oracle",
    model: str | None = None,
    backend: str = "docker",
    docker_image: str | None = None,
    agent_timeout: float = 120.0,
    verifier_timeout: float = 120.0,
    pass_threshold: float = 1.0,
    on_event: EventHook = None,
    environment_factory: Callable[..., "BaseEnvironment"] = make_environment,
    agent_factory: Callable[..., "BaseAgent"] = make_agent,
    max_infra_retries: int | None = None,
    platform_override: str | None = None,
) -> TrialOutcome:
    """Run one trial; retries setup-only infra failures (not after agent/LLM work)."""
    from app.core.config import settings
    from app.services.infra_retry import is_infra_retryable

    retries = settings.INFRA_RETRIES if max_infra_retries is None else max_infra_retries
    last: TrialOutcome | None = None
    for attempt in range(retries + 1):
        if attempt > 0:
            time.sleep(min(2**attempt, 8))
            log.info(
                "trial_infra_retry",
                extra={
                    "task": task_dir.name,
                    "attempt": attempt + 1,
                    "max_attempts": retries + 1,
                },
            )
        last = _run_trial_once(
            task_dir=task_dir,
            instruction=instruction,
            agent_name=agent_name,
            model=model,
            backend=backend,
            docker_image=docker_image,
            agent_timeout=agent_timeout,
            verifier_timeout=verifier_timeout,
            pass_threshold=pass_threshold,
            on_event=on_event,
            environment_factory=environment_factory,
            agent_factory=agent_factory,
            platform_override=platform_override,
        )
        if attempt >= retries or not is_infra_retryable(last):
            return last
    assert last is not None
    return last
