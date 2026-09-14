"""Rules-engine tests for app.services.diagnose (docs/DIAGNOSE.md Phase A).

Fixtures mirror the result.json trial shape written by trial_runner. Every
rule gets a positive and the clean-pass case proves silence.
"""

from __future__ import annotations

from app.services.diagnose import diagnose_result, diagnose_trial


def _step(command: str, *, phase: str = "agent", exit_code: int = 0, stdout: str = "", stderr: str = "") -> dict:
    return {
        "phase": phase,
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_s": 0.1,
    }


def _trial(
    *,
    passed: bool = False,
    steps: list | None = None,
    calls: int | None = None,
    agent_error: str | None = None,
    error: str | None = None,
    status: str = "completed",
    prompt_version: str = "bash-agent-v1",
    cost_usd: float | None = None,
) -> dict:
    agent_steps = [s for s in (steps or []) if s.get("phase") == "agent"]
    return {
        "n": 0,
        "status": status,
        "passed": passed,
        "reward": 1.0 if passed else 0.0,
        "error": error,
        "trajectory": {
            "agent": "anthropic",
            "model": "claude-sonnet-5",
            "prompt_version": prompt_version,
            "steps": steps or [],
            "agent_error": agent_error,
            "llm_usage_summary": {
                "calls": calls if calls is not None else len(agent_steps),
                "input_tokens": 100,
                "output_tokens": 20,
                "latency_ms": 50.0,
                "cost_usd": cost_usd,
            },
        },
    }


def _classes(fa: dict) -> set:
    return {o["failure_class"] for o in fa["occurrences"]}


# ---------------------------------------------------------------------------


def test_clean_passed_trial_has_no_findings():
    t = _trial(passed=True, steps=[_step("sed -i 's/a/b/' app.py"), _step("bash test.sh", phase="verifier")])
    fa = diagnose_trial(t)
    assert fa["status"] == "completed"
    assert fa["occurrences"] == []
    assert fa["primary"] is None


def test_infra_failure_from_trial_error():
    t = _trial(status="error", error="Agent network switch failed: bridge not connected")
    fa = diagnose_trial(t)
    assert "infra_failure" in _classes(fa)
    assert fa["primary"] == "infra_failure"


def test_step_budget_exhausted_definitive_with_max_steps():
    steps = [_step(f"grep -n x file{i}.py") for i in range(12)]
    t = _trial(steps=steps, calls=12)
    fa = diagnose_trial(t, max_steps=12)
    occ = [o for o in fa["occurrences"] if o["failure_class"] == "step_budget_exhausted"]
    assert occ and "==" in occ[0]["rationale"]


def test_step_budget_exhausted_inferred_without_max_steps():
    # calls == executed agent steps and no DONE -> heuristic fires, worded as likely
    steps = [_step(f"cat file{i}.py") for i in range(5)]
    t = _trial(steps=steps, calls=5)
    fa = diagnose_trial(t)
    occ = [o for o in fa["occurrences"] if o["failure_class"] == "step_budget_exhausted"]
    assert occ and "Likely" in occ[0]["title"]


def test_no_exhaustion_when_agent_said_done():
    # DONE = one extra LLM call with no executed step -> calls > steps
    steps = [_step("cat a.py"), _step("sed -i 's/x/y/' a.py")]
    t = _trial(steps=steps, calls=3)
    fa = diagnose_trial(t)
    assert "step_budget_exhausted" not in _classes(fa)


def test_never_edited_on_exploration_only_failure():
    steps = [_step("grep -rn TODO ."), _step("cat main.py"), _step("ls -la")]
    t = _trial(steps=steps, calls=4)  # calls != steps: not exhaustion
    fa = diagnose_trial(t)
    assert "never_edited" in _classes(fa)


def test_never_edited_not_fired_when_files_written():
    steps = [_step("cat main.py"), _step("echo 'fix' >> main.py")]
    t = _trial(steps=steps, calls=3)
    fa = diagnose_trial(t)
    assert "never_edited" not in _classes(fa)


def test_redirect_to_devnull_is_not_a_write():
    steps = [_step("command -v claude >/dev/null 2>&1"), _step("grep -c x a.py 2>/dev/null")]
    t = _trial(steps=steps, calls=3)
    fa = diagnose_trial(t)
    assert "never_edited" in _classes(fa)


def test_network_blocked_from_step_output():
    steps = [
        _step(
            "curl -fsSL https://example.com/install.sh",
            exit_code=6,
            stderr="curl: (6) Could not resolve host: example.com",
        )
    ]
    t = _trial(steps=steps, calls=2)
    fa = diagnose_trial(t)
    assert "network_blocked" in _classes(fa)


def test_time_budget_from_agent_error():
    t = _trial(steps=[_step("sleep 100")], agent_error="Agent session timed out after 900s.")
    fa = diagnose_trial(t)
    assert "time_budget_exhausted" in _classes(fa)


def test_gave_up_early_from_done_no_work():
    t = _trial(
        steps=[],
        calls=1,
        agent_error="LLM returned no runnable bash command after 1 call(s). (model replied DONE without running any command.)",
    )
    fa = diagnose_trial(t)
    assert "gave_up_early" in _classes(fa)


def test_read_test_file_flagged_on_passed_trial():
    steps = [_step("cat tests/test.sh"), _step("echo 42 > answer.txt")]
    t = _trial(passed=True, steps=steps, calls=3)
    fa = diagnose_trial(t)
    occ = [o for o in fa["occurrences"] if o["failure_class"] == "read_test_file"]
    assert occ and "Suspicious" in occ[0]["rationale"]


def test_verifier_tamper_attempt():
    steps = [_step("echo 'exit 0' > /tests/test.sh")]
    t = _trial(passed=True, steps=steps, calls=2)
    fa = diagnose_trial(t)
    assert "verifier_tamper_attempt" in _classes(fa)


def test_loop_repetition_three_identical_commands():
    steps = [_step("make build") for _ in range(3)] + [_step("ls")]
    t = _trial(steps=steps, calls=5)
    fa = diagnose_trial(t)
    occ = [o for o in fa["occurrences"] if o["failure_class"] == "loop_repetition"]
    assert occ and len(occ[0]["evidence_steps"]) == 3


def test_cost_blowout_only_with_limit():
    t = _trial(steps=[_step("sed -i s/a/b/ f.py")], calls=2, cost_usd=2.5)
    assert "cost_blowout" not in _classes(diagnose_trial(t))
    fa = diagnose_trial(t, cost_limit_usd=1.0)
    assert "cost_blowout" in _classes(fa)


def test_installed_agent_skips_command_rules():
    # claude-code trajectories record wrapper commands; command-level rules
    # must stay quiet even on failure.
    steps = [_step("curl https://claude.ai/install.sh | bash"), _step("claude --print ...")]
    t = _trial(steps=steps, calls=1, prompt_version="claude-code")
    fa = diagnose_trial(t)
    assert "never_edited" not in _classes(fa)
    assert "step_budget_exhausted" not in _classes(fa)


def test_diagnose_result_maps_all_trials_and_does_not_mutate():
    result = {"trials": [_trial(passed=True, steps=[_step("sed -i s/a/b/ f.py")]), _trial(steps=[_step("ls")], calls=2)]}
    before = str(result)
    analyses = diagnose_result(result)
    assert len(analyses) == 2
    assert str(result) == before
    assert "failure_analysis" not in result["trials"][0].get("trajectory", {})


def test_evidence_indices_point_into_full_steps_list():
    # Verifier step first -> agent step has absolute index 1.
    steps = [_step("bash setup.sh", phase="setup"), _step("grep x a.py")]
    t = _trial(steps=steps, calls=2)
    fa = diagnose_trial(t)
    never = [o for o in fa["occurrences"] if o["failure_class"] == "never_edited"]
    assert never and never[0]["evidence_steps"] == [1]


# ---------------------------------------------------------------------------
# Phase B — LLM extractor (app.services.diagnose_llm) with an injected llm_fn
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from app.services import llm as llm_mod
from app.services.diagnose_llm import diagnose_trial_full, extract


def _fake_call(text: str, cost: float | None = 0.001):
    def llm_fn(provider, model, system, user, **kw):
        return SimpleNamespace(
            text=text,
            provider=provider,
            model=model,
            input_tokens=500,
            output_tokens=60,
            latency_ms=200.0,
            cost_usd=cost,
        )

    return llm_fn


def test_extract_happy_path_validates_and_tags_llm():
    t = _trial(steps=[_step("grep x a.py"), _step("sed -i s/a/b/ wrong.py")], calls=3)
    reply = (
        '{"occurrences": [{"failure_class": "wrong_target", "title": "Edited the wrong file",'
        ' "rationale": "The fix belongs in b.py.", "evidence_steps": [1]}]}'
    )
    res = extract(t, "Fix the bug in b.py", llm_fn=_fake_call(reply))
    assert res.error is None
    assert len(res.occurrences) == 1
    occ = res.occurrences[0]
    assert occ["failure_class"] == "wrong_target"
    assert occ["detector"] == "llm"
    assert occ["evidence_steps"] == [1]
    assert res.llm_call["cost_usd"] == 0.001


def test_extract_drops_bad_evidence_and_maps_unknown_class():
    t = _trial(steps=[_step("ls")], calls=2)
    reply = (
        '{"occurrences": ['
        '{"failure_class": "made_up_class", "title": "T", "rationale": "R", "evidence_steps": [0]},'
        '{"failure_class": "wrong_target", "title": "No proof", "rationale": "R", "evidence_steps": [99]}'
        "]}"
    )
    res = extract(t, "task", llm_fn=_fake_call(reply))
    # Unknown class becomes "other"; the out-of-range-evidence claim is dropped.
    assert [o["failure_class"] for o in res.occurrences] == ["other"]


def test_extract_skips_classes_rules_already_found():
    t = _trial(steps=[_step("ls")], calls=2)
    reply = (
        '{"occurrences": [{"failure_class": "never_edited", "title": "T",'
        ' "rationale": "R", "evidence_steps": [0]}]}'
    )
    res = extract(t, "task", known_classes={"never_edited"}, llm_fn=_fake_call(reply))
    assert res.occurrences == []


def test_extract_unparseable_reply_keeps_call_meta():
    t = _trial(steps=[_step("ls")], calls=2)
    res = extract(t, "task", llm_fn=_fake_call("I could not decide."))
    assert res.error and "parse" in res.error
    assert res.llm_call is not None  # spend still recorded


def test_extract_provider_error_is_contained():
    def boom(*a, **k):
        raise llm_mod.ProviderError("no api key")

    t = _trial(steps=[_step("ls")], calls=2)
    res = extract(t, "task", llm_fn=boom)
    assert res.error == "no api key"
    assert res.llm_call is None


def test_full_diagnosis_merges_and_resorts_primary():
    # Rules find read_test_file (low severity); LLM adds misread_task (higher).
    steps = [_step("cat tests/test.sh"), _step("echo hi > out.txt")]
    t = _trial(passed=True, steps=steps, calls=3)
    reply = (
        '{"occurrences": [{"failure_class": "misread_task", "title": "Solved the wrong thing",'
        ' "rationale": "Output file does not match the ask.", "evidence_steps": [1]}]}'
    )
    fa = diagnose_trial_full(t, "Write a compressor", llm_fn=_fake_call(reply))
    assert fa["engine"] == "rules+llm"
    classes = [o["failure_class"] for o in fa["occurrences"]]
    assert "misread_task" in classes and "read_test_file" in classes
    # Severity re-sort: gave_up_early/misread_task-tier ranks above read_test_file.
    assert fa["primary"] == "misread_task"
    assert fa["llm_call"]["cost_usd"] == 0.001


def test_full_diagnosis_downgrades_to_rules_on_llm_failure():
    def boom(*a, **k):
        raise RuntimeError("network down")

    steps = [_step("grep x a.py")]
    t = _trial(steps=steps, calls=2)
    fa = diagnose_trial_full(t, "task", llm_fn=boom)
    assert fa["engine"] == "rules"
    assert "llm_error" in fa
    # Rules findings survive untouched.
    assert "never_edited" in {o["failure_class"] for o in fa["occurrences"]}
