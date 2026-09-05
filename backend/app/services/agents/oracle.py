"""
Oracle — the reference-solution agent.

Runs the task's own solution/solve.sh inside the container. Deterministic, free,
and calls no model, which makes it the agent every test and CI run uses: if the
oracle can't pass a task, the task itself is broken, not the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.models.enums import PhaseStatus
from app.services.agents.base import AgentResult, BaseAgent, EventHook
from app.services.environment import BaseEnvironment


class OracleAgent(BaseAgent):
    """Runs the task's own solution/solve.sh — deterministic, zero API calls,
    zero cost. This is the harness's self-test: it proves the sandbox +
    verifier + scoring pipeline works end to end without spending on a real
    model. Also genuinely useful standalone — "does this task's reference
    solution actually pass its own verifier" is a real, free check.

    Same interface as LLMAgent (.run(instruction, env, timeout, on_event) ->
    AgentResult) so trial_runner treats it identically to a real agent."""

    def __init__(self, task_dir: Path):
        self.task_dir = task_dir
        self.name = "oracle"

    def run(
        self,
        instruction: str,
        env: BaseEnvironment,
        timeout: Optional[float] = None,
        on_event: EventHook = None,
    ) -> AgentResult:
        solve_dir = self.task_dir / "solution"
        if not (solve_dir / "solve.sh").exists():
            return AgentResult(
                [],
                error=(
                    "No solution/solve.sh found. The oracle agent requires a shell script "
                    "at solution/solve.sh that applies the reference fix "
                    "(e.g. `cd /app && patch -p1 < /solution/fix.patch`)."
                ),
            )

        env.copy_in(solve_dir, "/solution")
        command = "bash /solution/solve.sh"
        solution_env = _load_solution_env(self.task_dir)
        if on_event:
            on_event(
                {
                    "type": "step",
                    "phase": "agent",
                    "command": command,
                    "status": PhaseStatus.RUNNING.value,
                }
            )
        result = env.exec(command, phase="agent", timeout=timeout, env=solution_env or None)
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
        error = None if result.exit_code == 0 else f"solve.sh exited {result.exit_code}"
        return AgentResult([result], error=error)


def _load_solution_env(task_dir: Path) -> dict:
    """Env for the oracle: solution/.env file and optional [solution].env in task.toml."""
    out: dict = {}
    env_file = task_dir / "solution" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                out[k] = v
    toml_path = task_dir / "task.toml"
    if toml_path.is_file():
        try:
            from app.services.task_parser import load_raw_toml

            raw = load_raw_toml(toml_path.read_bytes())
            sol = raw.get("solution") if isinstance(raw, dict) else None
            if isinstance(sol, dict):
                block = sol.get("env")
                if isinstance(block, dict):
                    for k, v in block.items():
                        out[str(k)] = str(v)
        except Exception:
            pass
    return out
