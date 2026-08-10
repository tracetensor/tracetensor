"""
Agent foundations — the contract every agent implements, and the shared parts.

An agent reads the instruction and works inside the environment (a Docker
container), producing a trajectory: the ordered commands it ran. It does NOT
score itself; the verifier does that afterwards.

Two families implement `BaseAgent`:

  * `LLMAgent` (llm_agent.py) — our own provider-agnostic bash loop. We drive the
    conversation: ask the model for the next command, run it, feed back the
    result, repeat.
  * `BaseInstalledAgent` subclasses (one module each) — a real third-party coding
    CLI installed and run INSIDE the sandbox. It drives itself; we install it,
    hand it the instruction, and read back its own trajectory.

Adding an agent: see registry.py. You write one module here and add one entry
there — nothing else in the codebase needs to change.
"""

from __future__ import annotations

import abc
import re
import time
from typing import Callable, List, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.models.enums import PhaseStatus
from app.services import llm
from app.services.environment import BaseEnvironment, ExecResult

log = get_logger("tracetensor.agent")

# on_event(evt: dict) -> None. Optional live-progress hook.
EventHook = Optional[Callable[[dict], None]]

# Noise markers the model sometimes echoes back from our feedback; cut at them.
_NOISE_MARKERS = ("Return the next bash command", "[exit ", "\n$ ")

# Defensive size caps on what feeds the prompt — a task's instruction.md is
# author-controlled content, not trusted input; an unbounded instruction is a
# cost vector (and a bigger surface for prompt injection). History is trimmed
# from the front (oldest turns) once it grows past budget, keeping the most
# recent context the agent actually needs to decide its next command.
MAX_INSTRUCTION_CHARS = 20_000
MAX_HISTORY_CHARS = 40_000

# ---------------------------------------------------------------------------
# LiteLLM model ids — shared by every agent that takes a "<provider>/<model>"
# ---------------------------------------------------------------------------

# Which Settings attribute holds each provider's key. Aliased from llm.py rather
# than restated: this mapping had drifted into four copies (two agents, the
# registry, and llm itself), and the symptom of missing one when a provider is
# added is a run that passes the key gate and then fails inside the container.
#
# The names double as env vars and as Settings attributes because config.py reads
# each `os.getenv(NAME)` into `self.NAME` — deliberate, so one table serves both.
LITELLM_PROVIDER_KEY_ATTR = llm.PROVIDER_KEY_ENV

#: What a bare model id (no "provider/" prefix) is assumed to be.
DEFAULT_LITELLM_PROVIDER = "anthropic"


def normalize_litellm_model(model: Optional[str], default: str) -> tuple[str, str]:
    """Return (full_model_id, provider) for a litellm-style agent.

    Accepts "anthropic/claude-haiku-4-5", a bare "claude-haiku-4-5" (assumed
    anthropic), or None (the agent's default).
    """
    resolved = model or default
    if "/" not in resolved:
        resolved = f"{DEFAULT_LITELLM_PROVIDER}/{resolved}"
    return resolved, resolved.split("/", 1)[0]


def litellm_api_key(provider: str, agent_name: str, model: str) -> tuple[str, str]:
    """The (env var name, key) this provider needs, or raise ProviderError.

    Raises rather than returning empty so a misconfigured agent fails at the
    point of use with a message naming the exact variable to set, instead of
    handing the container a blank key and failing inside the agent's own CLI.
    """
    from app.core.config import settings

    attr = LITELLM_PROVIDER_KEY_ATTR.get(provider)
    value = getattr(settings, attr, None) if attr else None
    if not value:
        raise llm.ProviderError(
            f"{agent_name} with model '{model}' needs {attr or 'a model key'} configured."
        )
    return attr, value  # type: ignore[return-value]  # attr is non-None when value is


# Node bootstrap shared by the JS-based CLIs (Claude Code / Gemini / Copilot need
# Node ≥20/≥22). python:3.11-slim has no Node, so install Node 22 (satisfies all
# of them) via NodeSource only when it's missing — a preinstalled or newer Node
# is left alone.
ENSURE_NODE = (
    "command -v node >/dev/null 2>&1 || { "
    "apt-get update -qq && apt-get install -y -qq curl ca-certificates && "
    "curl --proto '=https' --tlsv1.2 -fsSL https://deb.nodesource.com/setup_22.x | bash - && "
    "apt-get install -y -qq nodejs; }"
)


class AgentResult:
    def __init__(
        self,
        steps: List[ExecResult],
        error: Optional[str] = None,
        llm_calls: Optional[List[dict]] = None,
        prompt_version: Optional[str] = None,
        guardrail_flags: Optional[List[dict]] = None,
        raw_trajectory: Optional[dict] = None,
    ):
        self.steps = steps
        self.error = error
        # Per-call token/latency/cost records — an eval platform has to
        # instrument its own LLM spend. See app.services.llm.LLMCallResult.
        self.llm_calls = llm_calls or []
        self.prompt_version = prompt_version
        # Defense-in-depth output scan results (see app.services.guardrails).
        # The sandbox is the real defense; this is a loud tripwire on top of it.
        self.guardrail_flags = guardrail_flags or []
        # An external agent's own trajectory file (e.g. Mini-SWE's .traj.json),
        # captured verbatim so its full reasoning/steps are preserved even though
        # they ran inside the agent, not through our exec loop.
        self.raw_trajectory = raw_trajectory


class BaseAgent(abc.ABC):
    """What every agent must provide.

    Real abstract methods, not empty bodies: a contributor who subclasses this
    and forgets `run` gets a TypeError at instantiation, naming the method. The
    previous convention — a base class whose methods returned None — meant that
    same mistake produced a trial that silently did nothing and scored 0.0,
    indistinguishable from an agent that genuinely failed the task.
    """

    #: Stable label recorded on the job and understood by `make_agent`.
    name: str = "agent"

    #: Identifies the prompt/harness that produced a trajectory, so results
    #: recorded across versions stay comparable.
    PROMPT_VERSION: str = "unknown"

    @abc.abstractmethod
    def run(
        self,
        instruction: str,
        env: BaseEnvironment,
        timeout: Optional[float] = None,
        on_event: EventHook = None,
    ) -> "AgentResult":
        """Work on `instruction` inside `env` and return what happened.

        Must not raise for an ordinary agent failure — an agent that gives up,
        errors, or runs out of budget returns an AgentResult with `error` set.
        Raising is reserved for a broken configuration (a missing key), which the
        trial runner reports as a system error rather than a task failure.
        """


def _clean_command(text: str) -> str:
    """Turn the model's reply into a runnable command.

    Handles the common ways models garble a shell command: wrapping in code
    fences, prefixing a shell prompt ('$ ' / 'bash'), or echoing a whole fake
    transcript. We take the first real command line and strip the prompt.
    """
    t = text.strip()
    # Strip surrounding code fences / backticks.
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
    t = t.strip("`").strip()
    # Drop a leading language hint like "bash\n".
    t = re.sub(r"^bash\b[ \t]*\n?", "", t, count=1).strip()
    # Cut anything after an echoed-feedback marker.
    for marker in _NOISE_MARKERS:
        idx = t.find(marker)
        if idx > 0:
            t = t[:idx].strip()
    # Strip a leading shell prompt on the first line ('$ ' or 'bash ').
    t = re.sub(r"^\s*\$\s+", "", t)
    t = re.sub(r"^bash[ \t]+", "", t)
    return t.strip()


def _agent_exec_steps(steps: List[ExecResult]) -> List[ExecResult]:
    return [s for s in steps if getattr(s, "phase", None) == "agent"]


def _no_agent_work_error(llm_calls: List[dict], last_text: str) -> str:
    n = len(llm_calls)
    msg = f"LLM returned no runnable bash command after {n} call(s)."
    last = llm_calls[-1] if llm_calls else {}
    out_tok = last.get("output_tokens") or 0
    if out_tok > 100 and not last_text.strip():
        msg += (
            " (possible reasoning-model empty content — "
            "try a non-reasoning model or codex/claude-code.)"
        )
    elif last_text.strip().upper().startswith("DONE"):
        msg += " (model replied DONE without running any command.)"
    return msg


class BaseInstalledAgent(BaseAgent, abc.ABC):
    """An external coding agent installed and run INSIDE the sandbox (the standard
    BaseInstalledAgent pattern). Every installed agent shares one lifecycle —
    key check → install (budgeted) → run headless (remaining budget) → capture
    the agent's own trajectory → roll up its real spend — which this base owns.
    Subclasses only declare the differences via the hooks below.

    All installed agents call the model API *from inside* the container, so their
    tasks need network egress (network_mode = "public") and the key is injected
    through the process env (docker exec -e), never a logged command.
    """

    name: str = "installed-agent"
    PROMPT_VERSION: str = "installed-agent"

    #: Shell snippet that installs the agent into the container, run before the
    #: agent itself and charged against the same time budget.
    INSTALL: str = ""

    #: Per-run spend ceiling handed to agents that accept one. Overridable per
    #: agent; see settings.AGENT_COST_LIMIT_USD for the deployment-wide default.
    COST_LIMIT_USD: float = settings.AGENT_COST_LIMIT_USD

    # ---- subclass hooks ---------------------------------------------------
    @abc.abstractmethod
    def _secret_env(self) -> dict:
        """{ENV_NAME: api_key} the agent needs. Raise llm.ProviderError if the
        key isn't configured. Injected via the process env, never the command."""

    def _extra_env(self, env: BaseEnvironment) -> dict:
        """Non-secret env for the run (e.g. IS_SANDBOX=1). `env` is the container,
        so an agent can derive container-relative settings (e.g. its workdir).
        Default: none."""
        return {}

    @abc.abstractmethod
    def _run_command(self, instruction: str, env: BaseEnvironment) -> str:
        """The shell command that runs the agent headless on `instruction`.
        MUST shlex.quote the instruction (it's task-author content)."""

    def _masked_command(self) -> str:
        """An instruction-free label for the live event stream (no secrets)."""
        return self.name

    def _read_trajectory(self, env: BaseEnvironment, run: ExecResult) -> Optional[dict]:
        """The agent's own trajectory — from a file it wrote or its stdout."""
        return None

    def _llm_calls(self, raw: object) -> List[dict]:
        """Roll the trajectory up into our cost records. Default: none."""
        return []

    # ---- shared lifecycle -------------------------------------------------
    def run(
        self,
        instruction: str,
        env: BaseEnvironment,
        timeout: Optional[float] = None,
        on_event: EventHook = None,
    ) -> AgentResult:
        try:
            secret_env = self._secret_env()
        except llm.ProviderError as e:
            return AgentResult([], error=str(e), prompt_version=self.PROMPT_VERSION)

        # `timeout` is the TOTAL agent-session budget (standard semantics), shared
        # across install + run — not a per-command limit. One deadline so the two
        # phases can't each spend the full budget (2x wall-clock).
        deadline = (time.time() + timeout) if timeout else None

        def _remaining() -> Optional[float]:
            return max(0.0, deadline - time.time()) if deadline else None

        # 1. Install the agent into the container (needs network egress).
        if on_event:
            on_event(
                {
                    "type": "step",
                    "phase": "agent",
                    "command": self.INSTALL,
                    "status": PhaseStatus.RUNNING.value,
                }
            )
        install = env.exec(self.INSTALL, phase="agent", timeout=_remaining())
        steps = [install]
        if install.exit_code != 0:
            return AgentResult(
                steps,
                error=(
                    f"Could not install {self.name} in the sandbox — the task likely has no "
                    "network (installed agents need network_mode = 'public'). "
                    f"{install.stderr[-400:]}"
                ),
                prompt_version=self.PROMPT_VERSION,
            )

        # Install ate into the shared budget; bail if nothing's left rather than
        # calling exec with timeout<=0 (which would insta-timeout the run).
        remaining = _remaining()
        if remaining is not None and remaining <= 0:
            return AgentResult(
                steps,
                error=f"Agent session timed out after {timeout}s (during install).",
                prompt_version=self.PROMPT_VERSION,
            )

        # 2. Run headless. The key goes in via env, not the command. Installed
        #    agents may exit non-zero even on success (a cap hit, a noisy tool) —
        #    we don't gate on their exit code; the verifier is the source of truth.
        cmd = self._run_command(instruction, env)
        masked = self._masked_command()
        if on_event:
            on_event(
                {
                    "type": "step",
                    "phase": "agent",
                    "command": masked,
                    "status": PhaseStatus.RUNNING.value,
                }
            )
        run_env = {**secret_env, **self._extra_env(env)}
        run = env.exec(cmd, phase="agent", timeout=remaining, env=run_env)
        steps.append(run)
        if on_event:
            on_event(
                {
                    "type": "step",
                    "phase": "agent",
                    "command": masked,
                    "status": PhaseStatus.DONE.value,
                    "exit_code": run.exit_code,
                    "stdout": run.stdout[-2000:],
                    "stderr": run.stderr[-800:],
                }
            )

        # 3. Capture the agent's own trajectory + real spend so the trial's cost
        #    summary isn't a misleading zero (it spent money from the container).
        raw = self._read_trajectory(env, run)
        return AgentResult(
            steps,
            prompt_version=self.PROMPT_VERSION,
            raw_trajectory=raw,
            llm_calls=self._llm_calls(raw),
        )
