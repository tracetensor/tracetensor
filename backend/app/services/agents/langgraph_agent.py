"""LangGraph / Deep Agents — run a LangGraph project inside the sandbox.

The agent under test is not a CLI we install from a registry; it is *the user's
own code*. A LangGraph project is a directory holding a `langgraph.json`
registry plus the modules it points at:

    langgraph.json   {"dependencies": [...], "graphs": {"agent": "./agent.py:make_agent"}}
    agent.py         def make_agent(): -> a compiled LangGraph graph

so this adapter ships that directory into the container, installs what the
registry declares, and runs `_langgraph_runner.py` (the payload next to this
file) which resolves the graph reference and invokes the graph on the task's
instruction. The agent's own file tools do the work; the verifier grades the
files it left behind, exactly as for any other agent here.

Why the project comes from settings and not the task: the task defines the job,
the project defines the candidate. One task is meant to be run against many
candidate agents, so the project is a property of the *run* — Harbor passes it
as `--project-path`, we read `LANGGRAPH_PROJECT`.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import List, Optional

from app.core.config import settings
from app.services import llm
from app.services.agents.base import (
    BaseInstalledAgent,
    litellm_api_key,
    normalize_litellm_model,
)
from app.services.environment import BaseEnvironment, ExecResult

#: Where the project lands inside the sandbox. Outside the task's workdir so a
#: task that globs its own tree never picks up the agent's source as task data.
PROJECT_DIR = "/langgraph"
RUNNER_PATH = f"{PROJECT_DIR}/_tt_runner.py"
INSTRUCTION_PATH = f"{PROJECT_DIR}/instruction.txt"

BEGIN = "---TT-LANGGRAPH-RESULT-BEGIN---"
END = "---TT-LANGGRAPH-RESULT-END---"

DEFAULT_MODEL = "anthropic/claude-haiku-4-5"

#: Always installed — the graph runtime plus the provider integration matching
#: the model. The project's own `dependencies` are added on top.
_PROVIDER_PACKAGE = {
    "anthropic": "langchain-anthropic",
    "openai": "langchain-openai",
}


def _runner_source() -> bytes:
    return (Path(__file__).parent / "_langgraph_runner.py").read_bytes()


def _parse_result(stdout: Optional[str]) -> Optional[dict]:
    """The JSON object the runner printed between its markers.

    Marker-delimited rather than "parse the last line": LangChain, urllib3 and
    tokenizers all warn on stdout, and a run whose record we can't find would
    otherwise report zero tokens for a trial that really did spend money.
    """
    if not stdout:
        return None
    start = stdout.find(BEGIN)
    end = stdout.find(END, start + 1) if start != -1 else -1
    if start == -1 or end == -1:
        return None
    blob = stdout[start + len(BEGIN) : end].strip()
    try:
        obj = json.loads(blob)
    except (ValueError, TypeError):
        return {"raw": blob[:20000]}
    return obj if isinstance(obj, dict) else {"raw": blob[:20000]}


class LangGraphAgent(BaseInstalledAgent):
    """A LangGraph graph (incl. Deep Agents) built from a `langgraph.json`."""

    PROMPT_VERSION = "langgraph"
    # The graph runtime the project actually ran against — the closest thing a
    # LangGraph project has to an agent version.
    VERSION_COMMAND = (
        'python3 -c "import importlib.metadata as m; '
        "print('langgraph ' + m.version('langgraph'))\""
    )

    def __init__(self, project: Optional[str], model: Optional[str]):
        self.name = "langgraph"
        self.model, self.provider = normalize_litellm_model(model, DEFAULT_MODEL)
        # Bare model id for the agent code — a graph builds its own chat model and
        # wants "claude-haiku-4-5", not the litellm-prefixed id.
        self.bare_model = self.model.split("/", 1)[1]

        self._project_arg = project
        self.graph_name = settings.LANGGRAPH_GRAPH
        # Resolved in `run`, not here. Every registered agent must be constructible
        # from (task_dir, model) alone — the registry catalog builds one of each to
        # report status, and an agent that raised on a missing env var would take
        # that listing down. A missing project is reported two ways instead: by
        # `langgraph_key_check` before the run starts, and as a clean trial error.
        self.project: Optional[Path] = None
        self.registry: dict = {}

    def _resolve_project(self) -> None:
        raw = self._project_arg or settings.LANGGRAPH_PROJECT
        if not raw:
            raise llm.ProviderError(
                "langgraph needs a project directory — set LANGGRAPH_PROJECT to the "
                "folder containing langgraph.json (Harbor's --project-path)."
            )
        project = Path(raw).expanduser().resolve()
        if not (project / "langgraph.json").exists():
            raise llm.ProviderError(
                f"No langgraph.json in {project} — LANGGRAPH_PROJECT must point at "
                "a LangGraph project directory."
            )
        self.project = project
        self.registry = json.loads((project / "langgraph.json").read_text("utf-8"))
        # Instance attribute shadows BaseInstalledAgent.INSTALL: what to install is
        # a property of this project, not of the class.
        self.INSTALL = self._install_script()

    # ---- install ----------------------------------------------------------
    def _declared_dependencies(self) -> List[str]:
        """pip targets from langgraph.json.

        `"."` means "install the project itself", which needs packaging metadata
        the project may not have. We put the project on sys.path in the runner
        instead and skip it here — same import result for a flat project, no
        build step. Anything else is passed to pip verbatim.
        """
        deps = self.registry.get("dependencies") or []
        return [d for d in deps if isinstance(d, str) and d.strip() not in (".", "./")]

    def _install_script(self) -> str:
        pkgs = ["langgraph", _PROVIDER_PACKAGE.get(self.provider, "")]
        pkgs += self._declared_dependencies()
        wanted = " ".join(shlex.quote(p) for p in pkgs if p)
        # --break-system-packages: Debian-based images mark their Python as
        # externally managed (PEP 668) and refuse a plain `pip install`. The
        # container is disposable, so overriding is correct here.
        return (
            "(command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1) || "
            "{ apt-get update -qq && apt-get install -y -qq python3-pip; }; "
            "PIP=$(command -v pip3 || command -v pip); "
            f"$PIP install -q --break-system-packages {wanted} 2>/dev/null "
            f"|| $PIP install -q {wanted}"
        )

    # ---- run --------------------------------------------------------------
    def _secret_env(self) -> dict:
        attr, key = litellm_api_key(self.provider, self.name, self.model)
        return {attr: key}

    def _extra_env(self, env: BaseEnvironment) -> dict:
        cfg = {
            "TT_LANGGRAPH_PROJECT": PROJECT_DIR,
            "TT_INSTRUCTION_FILE": INSTRUCTION_PATH,
            "TT_WORKDIR": getattr(env, "workdir", "/app") or "/app",
            "TT_RECURSION_LIMIT": str(settings.LANGGRAPH_RECURSION_LIMIT),
            # The run's model choice, so `-m` reaches the graph's own chat model.
            "TT_MODEL": self.bare_model,
            "ANTHROPIC_MODEL": self.bare_model,
        }
        if self.graph_name:
            cfg["TT_LANGGRAPH_GRAPH"] = self.graph_name
        return cfg

    def _run_command(self, instruction: str, env: BaseEnvironment) -> str:
        # The instruction travels as a file (written in `run`), not an argv string:
        # it is task-author content, it can be long, and keeping it out of the
        # command means it never lands in a step log or an event payload.
        return f"python3 {shlex.quote(RUNNER_PATH)}"

    def _masked_command(self) -> str:
        graph = self.graph_name or "default"
        return f"python3 _tt_runner.py  (graph={graph}, model={self.bare_model})"

    def run(self, instruction, env, timeout=None, on_event=None):
        """Stage the project, then hand off to the shared installed-agent lifecycle.

        Staging has to precede INSTALL because the install script may reference
        the project's own declared dependencies.
        """
        from app.services.agents.base import AgentResult

        try:
            self._resolve_project()
        except llm.ProviderError as exc:
            # Same shape the base class gives a missing API key: a task failure to
            # report, not an exception for the trial runner to classify.
            return AgentResult([], error=str(exc), prompt_version=self.PROMPT_VERSION)

        try:
            env.copy_in(self.project, PROJECT_DIR)
            env.write_file(RUNNER_PATH, _runner_source())
            env.write_file(INSTRUCTION_PATH, instruction.encode("utf-8"))
            # Both backends write these root-owned (Docker via `docker cp` of a
            # 0600 temp file). A task whose agent user isn't root could not
            # otherwise read its own runner.
            env.exec(f"chmod -R a+rX {shlex.quote(PROJECT_DIR)}", phase="setup", as_user="0")
        except Exception as exc:  # staging failure is ours, not the agent's
            return AgentResult(
                [],
                error=f"Could not stage the LangGraph project into the sandbox: {exc}",
                prompt_version=self.PROMPT_VERSION,
            )
        return super().run(instruction, env, timeout=timeout, on_event=on_event)

    # ---- results ----------------------------------------------------------
    def _read_trajectory(self, env: BaseEnvironment, run: ExecResult) -> Optional[dict]:
        return _parse_result(run.stdout)

    def _llm_calls(self, raw: object) -> List[dict]:
        if not isinstance(raw, dict):
            return []
        usage = raw.get("usage")
        if not isinstance(usage, dict) or not usage.get("api_calls"):
            return []
        return [
            {
                "provider": self.provider,
                "model": usage.get("model") or self.bare_model,
                "input_tokens": usage.get("input_tokens") or None,
                "output_tokens": usage.get("output_tokens") or None,
                "latency_ms": None,
                # LangChain reports tokens, not dollars. Leaving cost None is
                # honest; vault prices it from tokens when the model is known.
                "cost_usd": None,
                "api_calls": usage.get("api_calls"),
            }
        ]
