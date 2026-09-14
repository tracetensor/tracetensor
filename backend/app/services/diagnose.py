"""
Diagnose — deterministic failure classification over a recorded trial.

Phase A of docs/DIAGNOSE.md: pure functions, no LLM, no DB. Input is a trial
dict in the shape trial_runner records (and `runs/*/result.json` stores);
output is a `failure_analysis` block the caller may attach to
`trajectory.failure_analysis` (the Trajectory schema allows extra keys on
purpose). Nothing here mutates its input.

The verifier already labels pass/fail — these rules only explain *why*, and
flag suspicious behavior on passed trials (an agent that read the test file
before "solving" the task is a task-quality problem, not a win).

Honesty rules carried over from the rest of the codebase:
  * Every occurrence must cite evidence (step indices into trajectory.steps);
    a claim with no evidence is dropped, same spirit as guardrails recording
    the exact command.
  * Heuristics say so in their rationale. `step_budget_exhausted` without a
    known max_steps is inferred from call/step accounting and worded as
    "likely", not stated as fact.
  * Command-level rules only run for the built-in bash loop, where each step
    IS one agent-chosen command (prompt_version "bash-agent-*"). Installed
    agents (claude-code, codex, …) record wrapper commands; pretending to
    read their intent from those would be noise.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.enums import DiagnosisStatus

ENGINE = "rules"

#: The full failure taxonomy (docs/DIAGNOSE.md), in severity order — used both
#: to pick the one-line `primary` class and to constrain what the Phase B LLM
#: extractor may claim. Classes after `loop_repetition` are LLM-detected only;
#: they live here so there is exactly one list of valid labels.
SEVERITY_ORDER = [
    "infra_failure",
    "network_blocked",
    "verifier_tamper_attempt",
    "time_budget_exhausted",
    "step_budget_exhausted",
    "gave_up_early",
    "misread_task",
    "wrong_target",
    "env_assumption",
    "never_edited",
    "loop_repetition",
    "read_test_file",
    "cost_blowout",
    "other",
]

# --- command shape patterns (bash-loop steps only) --------------------------

#: Redirections that are reads-with-plumbing, not writes. Stripped before
#: looking for a real `>` file write.
_DEVNULL_RE = re.compile(r"(?:\d?>>?\s*/dev/null|\d>&\d|&>\s*/dev/null)")

#: Commands that modify files. Coarse on purpose (same stance as guardrails):
#: a false positive costs a wrong hint, not a wrong grade.
_WRITE_RE = re.compile(
    r"(?:^|\s|;|&&|\|\|)\s*"
    r"(?:sed\s+(?:-\S+\s+)*-i|tee\s|patch\b|git\s+apply|mv\s|cp\s|rm\s|"
    r"mkdir\s|touch\s|chmod\s|ln\s|truncate\s|dd\s|install\s)"
)
_REDIRECT_WRITE_RE = re.compile(r">>?\s*\S")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?\w+")

#: Paths that belong to the grader, not the task.
_TEST_PATH_RE = re.compile(r"(?:^|[\s'\"/])tests?/|/tests/|\btest\.sh\b|\breward\.(?:txt|json|toml)\b")
_READ_CMD_RE = re.compile(r"(?:^|\s|;|&&|\|\|)\s*(?:cat|grep|less|more|head|tail|nl|strings|awk|sed\s+-n)\b")

_NETWORK_ERR_RE = re.compile(
    r"Could not resolve|Temporary failure in name resolution|Network is unreachable|"
    r"Connection refused|Connection timed out|curl: \(6\)|curl: \(7\)|"
    r"getaddrinfo|ENOTFOUND|EAI_AGAIN",
    re.IGNORECASE,
)


def _occurrence(
    failure_class: str, title: str, rationale: str, evidence_steps: List[int]
) -> Dict[str, Any]:
    return {
        "failure_class": failure_class,
        "title": title,
        "rationale": rationale,
        "evidence_steps": evidence_steps,
        "detector": "rule",
    }


def _is_bash_loop(trajectory: Dict[str, Any]) -> bool:
    return str(trajectory.get("prompt_version") or "").startswith("bash-agent")


def _agent_steps(trajectory: Dict[str, Any]) -> List[tuple]:
    """(absolute index, step dict) for agent-phase steps."""
    out = []
    for i, s in enumerate(trajectory.get("steps") or []):
        if isinstance(s, dict) and s.get("phase") == "agent":
            out.append((i, s))
    return out


def _writes_files(command: str) -> bool:
    cmd = _DEVNULL_RE.sub(" ", command)
    return bool(
        _WRITE_RE.search(cmd) or _REDIRECT_WRITE_RE.search(cmd) or _HEREDOC_RE.search(cmd)
    )


def _norm(command: str) -> str:
    return " ".join(command.split())


def diagnose_trial(
    trial: Dict[str, Any],
    *,
    max_steps: Optional[int] = None,
    cost_limit_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """Classify one recorded trial. Returns a `failure_analysis` block.

    `trial` is the dict shape written by trial_runner / result.json:
    status, passed, reward, error, trajectory{steps, agent_error, llm_calls,
    llm_usage_summary, prompt_version, …}. Missing keys are tolerated —
    older artifacts must still diagnose.
    """
    trajectory = trial.get("trajectory") or {}
    passed = bool(trial.get("passed"))
    occurrences: List[Dict[str, Any]] = []

    agent_steps = _agent_steps(trajectory)
    bash_loop = _is_bash_loop(trajectory)

    # -- infra: the platform, not the agent, failed --------------------------
    trial_error = trial.get("error")
    if trial.get("status") == "error" or trial_error:
        occurrences.append(
            _occurrence(
                "infra_failure",
                "Trial failed before/outside agent work",
                f"trial.error: {str(trial_error)[:200]}",
                [],
            )
        )

    # -- agent session errors (recorded by the bash loop) --------------------
    agent_error = str(trajectory.get("agent_error") or "")
    if agent_error:
        low = agent_error.lower()
        if "timed out" in low:
            occurrences.append(
                _occurrence(
                    "time_budget_exhausted",
                    "Agent session hit its time budget",
                    agent_error[:200],
                    [i for i, _ in agent_steps[-1:]],
                )
            )
        elif "done" in low and "no runnable" in low.replace("returned no runnable", "no runnable"):
            occurrences.append(
                _occurrence(
                    "gave_up_early",
                    "Model replied DONE without doing any work",
                    agent_error[:200],
                    [],
                )
            )
        elif "no runnable" in low:
            occurrences.append(
                _occurrence(
                    "other",
                    "Model produced no runnable command",
                    agent_error[:200],
                    [],
                )
            )

    # -- network: commands failed for lack of egress -------------------------
    net_hits = [
        i
        for i, s in agent_steps
        if _NETWORK_ERR_RE.search((s.get("stderr") or "") + (s.get("stdout") or ""))
    ]
    if net_hits:
        mode = (trajectory.get("execution") or {}).get("network_mode")
        suffix = f" (network_mode={mode})" if mode else ""
        occurrences.append(
            _occurrence(
                "network_blocked",
                "Commands failed with network errors",
                f"{len(net_hits)} step(s) show resolver/connect failures{suffix}.",
                net_hits,
            )
        )

    # -- grader-directed behavior (fires on passed trials too) ---------------
    if bash_loop:
        tamper_hits, read_hits = [], []
        for i, s in agent_steps:
            cmd = s.get("command") or ""
            if not _TEST_PATH_RE.search(cmd):
                continue
            if _writes_files(cmd):
                tamper_hits.append(i)
            elif _READ_CMD_RE.search(cmd):
                read_hits.append(i)
        if tamper_hits:
            occurrences.append(
                _occurrence(
                    "verifier_tamper_attempt",
                    "Agent modified grader files",
                    f"{len(tamper_hits)} command(s) write into tests/ or reward files.",
                    tamper_hits,
                )
            )
        if read_hits:
            note = (
                "Suspicious on a passed trial: the agent may have fit the test, not the task."
                if passed
                else "Recorded for context; reading tests is sometimes legitimate strategy."
            )
            occurrences.append(
                _occurrence(
                    "read_test_file",
                    "Agent read grader files",
                    f"{len(read_hits)} command(s) read tests/ or reward files. {note}",
                    read_hits,
                )
            )

    # -- cost ----------------------------------------------------------------
    usage = trajectory.get("llm_usage_summary") or {}
    cost = usage.get("cost_usd")
    if cost_limit_usd is not None and isinstance(cost, (int, float)) and cost > cost_limit_usd:
        occurrences.append(
            _occurrence(
                "cost_blowout",
                "LLM spend exceeded the trial budget",
                f"cost_usd {cost:.4f} > limit {cost_limit_usd:.4f}.",
                [],
            )
        )

    # -- failure-only rules (why a failed trial failed) ----------------------
    if not passed and bash_loop and agent_steps:
        calls = usage.get("calls") or 0

        # Budget exhaustion. Definitive when max_steps is known; otherwise
        # inferred: the loop ends via DONE (an LLM call with no executed
        # step) or by running out of range — so calls == executed agent
        # steps, with no session error, means no DONE ever arrived.
        n = len(agent_steps)
        if max_steps is not None and n >= max_steps:
            occurrences.append(
                _occurrence(
                    "step_budget_exhausted",
                    "Ran out of steps",
                    f"{n} agent steps == max_steps {max_steps}; session ended without DONE.",
                    [agent_steps[0][0], agent_steps[-1][0]],
                )
            )
        elif max_steps is None and not agent_error and calls and calls == n:
            occurrences.append(
                _occurrence(
                    "step_budget_exhausted",
                    "Likely ran out of steps",
                    f"All {calls} LLM calls produced executed commands and the session "
                    "ended without a DONE reply — consistent with hitting the step budget.",
                    [agent_steps[0][0], agent_steps[-1][0]],
                )
            )

        # Never edited anything: exploration-only trajectories.
        if not any(_writes_files(s.get("command") or "") for _, s in agent_steps):
            occurrences.append(
                _occurrence(
                    "never_edited",
                    "No file was ever modified",
                    f"None of the {len(agent_steps)} agent commands write files — "
                    "the run was read/exploration only.",
                    [i for i, _ in agent_steps],
                )
            )

        # Loops: >=3 consecutive identical commands.
        streak_start, streak = 0, 1
        cmds = [(i, _norm(s.get("command") or "")) for i, s in agent_steps]
        for k in range(1, len(cmds)):
            if cmds[k][1] and cmds[k][1] == cmds[k - 1][1]:
                streak += 1
            else:
                streak_start, streak = k, 1
            if streak == 3:
                occurrences.append(
                    _occurrence(
                        "loop_repetition",
                        "Repeated the same command",
                        f"Command `{cmds[k][1][:80]}` ran 3+ times in a row.",
                        [cmds[j][0] for j in range(streak_start, k + 1)],
                    )
                )
                break

    occurrences.sort(key=lambda o: SEVERITY_ORDER.index(o["failure_class"]))
    return {
        "status": DiagnosisStatus.COMPLETED.value,
        "engine": ENGINE,
        "diagnosed_at": datetime.now(timezone.utc).isoformat(),
        "primary": occurrences[0]["failure_class"] if occurrences else None,
        "occurrences": occurrences,
    }


def diagnose_result(
    result: Dict[str, Any],
    *,
    max_steps: Optional[int] = None,
    cost_limit_usd: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Diagnose every trial of a result.json-shaped dict. Returns one
    failure_analysis block per trial, in trial order. Does not mutate."""
    return [
        diagnose_trial(t, max_steps=max_steps, cost_limit_usd=cost_limit_usd)
        for t in (result.get("trials") or [])
        if isinstance(t, dict)
    ]
