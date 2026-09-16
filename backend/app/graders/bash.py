"""
BashGrader — score by running a shell command.

Runs `/bin/bash -lc <command>` in a subprocess. The process is given its own
process group so the entire tree can be killed cleanly on timeout.

Scoring:
  exit code 0  → 1.0
  exit code ≠ 0 → 0.0 (reason includes exit code + stderr snippet)
  timeout      → 0.0 (reason names the timeout)

Default timeout is 600s (mirrors HUD's BashGrader). Pass timeout=N to override.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from app.graders.base import EvaluationResult, SubScore

_DEFAULT_TIMEOUT = 600.0
_OUT_LIMIT = 2000
_ERR_LIMIT = 500


def _kill_pgroup(pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


class BashGrader:
    """Score an agent output by running a shell command.

    Usage in an env.py generator::

        @env.template(id="run-tests")
        async def run_tests():
            answer = yield "Run the tests and report the output."
            result = await BashGrader.grade(
                command="cd /workspace && bash tests/test.sh",
                timeout=60,
            )
            yield float(result)

    The command sees the agent's answer as the ``AGENT_OUTPUT`` environment
    variable so the script can inspect it without pipe tricks.
    """

    DEFAULT_TIMEOUT: float = _DEFAULT_TIMEOUT

    @classmethod
    async def grade(
        cls,
        *,
        command: str,
        weight: float = 1.0,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        env_extras: dict[str, str] | None = None,
    ) -> EvaluationResult:
        timeout = timeout if timeout is not None else cls.DEFAULT_TIMEOUT

        extra_env = dict(os.environ)
        if env_extras:
            extra_env.update(env_extras)

        preexec = os.setsid if hasattr(os, "setsid") else None

        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", "-lc", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=extra_env,
                preexec_fn=preexec,
            )
        except FileNotFoundError:
            return EvaluationResult(
                score=0.0, reason="BashGrader: /bin/bash not found",
                subscores=[SubScore(name="bash", score=0.0, weight=weight,
                                    metadata={"command": command[:200], "error": "bash not found"})],
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_pgroup(proc.pid)
            return EvaluationResult(
                score=0.0, reason=f"BashGrader: timed out after {timeout}s",
                subscores=[SubScore(name="bash", score=0.0, weight=weight,
                                    metadata={"command": command[:200], "timeout": timeout})],
            )

        rc = proc.returncode
        stdout = (stdout_b or b"").decode(errors="replace")[:_OUT_LIMIT]
        stderr = (stderr_b or b"").decode(errors="replace")[:_ERR_LIMIT]
        score = 1.0 if rc == 0 else 0.0
        reason = f"exit={rc}"
        if stderr:
            reason += f" stderr={stderr!r}"
        if stdout and rc != 0:
            reason += f" stdout={stdout!r}"
        return EvaluationResult(
            score=score,
            reason=reason,
            subscores=[SubScore(
                name="bash",
                score=score,
                weight=weight,
                metadata={"exit_code": rc, "command": command[:200],
                          "stdout": stdout[:500], "stderr": stderr[:200]},
            )],
        )
