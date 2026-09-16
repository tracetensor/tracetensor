"""
HUD-compat integration tests.

Tests two things:
  1. The format detector correctly identifies HUD-style tasks.
  2. The HUD runner can execute HUD-style tasks end-to-end with a real LLM.

Run with:
  cd backend && python -m pytest tests/test_hud_compat.py -v

Requires OPENAI_API_KEY in backend/.env (or env).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

BLANK_DIR = Path(__file__).parent / "hud_compat" / "blank"


# ── Format detector ──────────────────────────────────────────────────


def test_detect_hud_format():
    from app.services.format_detector import detect_format
    assert detect_format(BLANK_DIR) == "hud"


def test_detect_tracetensor_format(tmp_path):
    (tmp_path / "task.toml").write_text('[task]\nname = "test"')
    from app.services.format_detector import detect_format
    assert detect_format(tmp_path) == "tracetensor"


def test_detect_tracetensor_wins_when_both(tmp_path):
    """task.toml takes priority if both files exist."""
    (tmp_path / "task.toml").write_text('[task]\nname = "test"')
    (tmp_path / "env.py").write_text("from hud import Environment\nenv = Environment('x')")
    from app.services.format_detector import detect_format
    assert detect_format(tmp_path) == "tracetensor"


def test_detect_unknown_raises(tmp_path):
    from app.services.format_detector import UnknownTaskFormat, detect_format
    with pytest.raises(UnknownTaskFormat):
        detect_format(tmp_path)


# ── HUD adapter (loading only, no LLM) ──────────────────────────────


def test_load_hud_env_blank():
    from app.services.hud_adapter import load_hud_env
    result = load_hud_env(BLANK_DIR)

    assert result.env.name == "blank"
    assert "count" in result.env.templates
    assert len(result.tasks) == 3


def test_bound_task_fields():
    from app.services.hud_adapter import load_hud_env
    result = load_hud_env(BLANK_DIR)

    t = result.tasks[0]
    assert t.template_id == "count"
    assert "sentence" in t.kwargs
    assert "letter" in t.kwargs


def test_hud_task_prompt_generation():
    """First yield of the generator produces the prompt — no LLM needed."""
    import asyncio
    from app.services.hud_adapter import load_hud_env

    result = load_hud_env(BLANK_DIR)
    task = result.tasks[0]  # count(sentence="Strawberry world", letter="r")

    async def _get_prompt():
        gen = task.fn(**task.kwargs)
        return await gen.asend(None)

    prompt = asyncio.run(_get_prompt())
    assert "Strawberry world" in prompt
    assert "'r'" in prompt


def test_hud_task_inline_grading():
    """Second yield grades the answer correctly — no LLM needed."""
    import asyncio
    from app.services.hud_adapter import load_hud_env

    result = load_hud_env(BLANK_DIR)

    # sentence="Strawberry world", letter="r" → "strawberry world".count("r") = 4
    task = result.tasks[0]

    async def _run():
        gen = task.fn(**task.kwargs)
        await gen.asend(None)          # consume prompt
        score = await gen.asend("4")   # correct answer
        return score

    score = asyncio.run(_run())
    assert float(score) == 1.0


def test_hud_task_inline_grading_wrong():
    """Wrong answer scores 0.0."""
    import asyncio
    from app.services.hud_adapter import load_hud_env

    result = load_hud_env(BLANK_DIR)
    task = result.tasks[0]

    async def _run():
        gen = task.fn(**task.kwargs)
        await gen.asend(None)
        return await gen.asend("99")  # wrong

    score = asyncio.run(_run())
    assert float(score) == 0.0


# ── Capability base ──────────────────────────────────────────────────


def test_capability_browser_factory():
    from app.capabilities.base import Capability
    cap = Capability.browser(url="localhost:9222")
    assert cap.protocol == "cdp/1.3"
    assert "9222" in cap.url


def test_capability_desktop_factory():
    from app.capabilities.base import Capability
    cap = Capability.desktop(url="localhost:5900")
    assert cap.protocol == "rfb/3.8"
    assert cap.params["display"] == 0


def test_capability_mcp_factory():
    from app.capabilities.base import Capability
    cap = Capability.mcp(url="http://localhost:8040/mcp")
    assert cap.protocol == "mcp/2025-11-25"
    assert cap.params["transport"] == "streamable-http"


def test_capability_shell_factory():
    from app.capabilities.base import Capability
    cap = Capability.shell()
    assert cap.protocol == "shell/exec"


def test_capability_manifest_roundtrip():
    from app.capabilities.base import Capability
    cap = Capability.browser(url="ws://localhost:9222", target_id="abc")
    restored = Capability.from_manifest(cap.to_manifest())
    assert restored.protocol == cap.protocol
    assert restored.url == cap.url
    assert restored.params == cap.params


# ── HUD stub environment ─────────────────────────────────────────────


def test_stub_environment_template_registration():
    from app.services.hud_compat import TensorEnvironment

    env = TensorEnvironment(name="test")

    @env.template(id="my_task")
    async def my_task(x: str):
        answer = yield f"What is {x}?"
        yield 1.0 if answer == "42" else 0.0

    assert "my_task" in env.templates


def test_stub_environment_bound_task():
    from app.services.hud_compat import BoundTask, TensorEnvironment

    env = TensorEnvironment(name="test")

    @env.template(id="add")
    async def add(a: int, b: int):
        answer = yield f"{a} + {b} = ?"
        yield 1.0 if answer.strip() == str(a + b) else 0.0

    task = add(a=2, b=3)
    assert isinstance(task, BoundTask)
    assert task.kwargs == {"a": 2, "b": 3}


def test_stub_inject_into_sysmodules():
    """After inject_hud_stubs(), `from hud import Environment` works."""
    import sys
    from app.services.hud_compat import TensorEnvironment, inject_hud_stubs

    inject_hud_stubs()
    assert "hud" in sys.modules
    hud_mod = sys.modules["hud"]
    assert hud_mod.Environment is TensorEnvironment


# ── Real LLM integration test (requires OPENAI_API_KEY) ─────────────


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set"
)
def test_hud_run_blank_with_openai():
    """Run the blank env tasks end-to-end with a real OpenAI call."""
    from app.services.hud_adapter import load_hud_env
    from app.services.hud_runner import run_hud_task_sync
    from app.models.enums import TrialStatus

    result = load_hud_env(BLANK_DIR)

    # Run just the first task: count "r" in "Strawberry world" → correct answer is 3
    task = result.tasks[0]
    outcome = run_hud_task_sync(
        task,
        provider="openai",
        model="gpt-4o-mini",
        timeout=30.0,
    )

    print(f"\n[HUD blank test] reward={outcome.reward} answer={outcome.trajectory.get('answer')}")
    assert outcome.status == TrialStatus.COMPLETED.value
    assert outcome.reward is not None
    # We don't assert reward == 1.0 (LLM might count wrong) but we assert
    # the pipeline ran and returned a valid 0-1 score.
    assert 0.0 <= outcome.reward <= 1.0


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set"
)
def test_hud_run_all_blank_tasks():
    """Run all 3 blank tasks. Expect at least 2/3 correct from gpt-4o-mini."""
    from app.services.hud_adapter import load_hud_env
    from app.services.hud_runner import run_hud_task_sync
    from app.models.enums import TrialStatus

    result = load_hud_env(BLANK_DIR)
    scores = []

    for task in result.tasks:
        outcome = run_hud_task_sync(
            task,
            provider="openai",
            model="gpt-4o-mini",
            timeout=30.0,
        )
        assert outcome.status == TrialStatus.COMPLETED.value
        scores.append(outcome.reward or 0.0)
        print(
            f"  task={task.kwargs} "
            f"answer={outcome.trajectory.get('answer')!r} "
            f"reward={outcome.reward}"
        )

    mean = sum(scores) / len(scores)
    print(f"\n[HUD blank] mean reward={mean:.2f} on {len(scores)} tasks")
    assert mean >= 0.5, f"Expected ≥50% correct from gpt-4o-mini, got {mean:.2f}"
