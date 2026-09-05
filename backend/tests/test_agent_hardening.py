"""
Tests for A and D — installed-agent hardening.

A: _capture_version tries VERSION_COMMAND_FALLBACK when primary fails.
D: BaseInstalledAgent.run() truncates instructions exceeding MAX_INSTRUCTION_CHARS.

These tests use FakeEnvironment so no Docker required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeEnvironment
from app.services.agents.base import (
    MAX_INSTRUCTION_CHARS,
    AgentResult,
    BaseInstalledAgent,
)
from app.services.environment import ExecResult


# ---------------------------------------------------------------------------
# Minimal concrete subclass — only what's needed for the tests.
# ---------------------------------------------------------------------------

class MinimalInstalledAgent(BaseInstalledAgent):
    name = "test-agent"
    INSTALL = "echo installed"
    PROMPT_VERSION = "test-v1"

    def __init__(self, model=None):
        self.model = model

    def _secret_env(self):
        return {}

    def _run_command(self, instruction, env):
        return "echo ok"


# ---------------------------------------------------------------------------
# Item A — version probe fallback
# ---------------------------------------------------------------------------

def _make_agent(**overrides):
    """Create a fresh MinimalInstalledAgent with class-level attribute overrides."""
    attrs = {"VERSION_COMMAND": None, "VERSION_COMMAND_FALLBACK": None, **overrides}
    cls = type("Agent", (MinimalInstalledAgent,), attrs)
    return cls()


class TestVersionProbe:
    """_capture_version uses fallback when primary VERSION_COMMAND fails."""

    def test_primary_succeeds_returns_version(self):
        agent = _make_agent(VERSION_COMMAND="mytool --version")
        env = FakeEnvironment(Path("."), responses={"--version": {"exit_code": 0, "stdout": "1.2.3"}})
        assert agent._capture_version(env) == "1.2.3"

    def test_primary_fails_fallback_succeeds(self):
        agent = _make_agent(
            VERSION_COMMAND="mytool version",
            VERSION_COMMAND_FALLBACK="mytool --version",
        )

        class SmartEnv(FakeEnvironment):
            def exec(self, command, phase="agent", timeout=None, as_user=None, env=None):
                if "--version" in command:
                    return ExecResult(command=command, exit_code=0, stdout="2.0.0",
                                      stderr="", duration_s=0.0, phase=phase)
                return ExecResult(command=command, exit_code=1, stdout="",
                                  stderr="unknown subcommand", duration_s=0.0, phase=phase)

        assert agent._capture_version(SmartEnv(Path("."))) == "2.0.0"

    def test_primary_fails_no_fallback_returns_none(self):
        agent = _make_agent(VERSION_COMMAND="mytool version")
        env = FakeEnvironment(Path("."), responses={"mytool version": {"exit_code": 1}})
        assert agent._capture_version(env) is None

    def test_no_version_command_returns_none(self):
        agent = _make_agent(VERSION_COMMAND=None)
        assert agent._capture_version(FakeEnvironment(Path("."))) is None

    def test_exception_in_exec_does_not_raise(self):
        class BrokenEnv(FakeEnvironment):
            def exec(self, command, **kwargs):
                raise RuntimeError("container not found")

        agent = _make_agent(VERSION_COMMAND="mytool --version")
        assert agent._capture_version(BrokenEnv(Path("."))) is None


# ---------------------------------------------------------------------------
# Item D — prompt size guard for installed agents
# ---------------------------------------------------------------------------

class TestInstalledAgentPromptTruncation:
    """BaseInstalledAgent.run() truncates instructions over MAX_INSTRUCTION_CHARS."""

    def _run_with_instruction(self, instruction: str):
        """Run the agent and return (result, instruction_seen_by_run_command)."""
        seen: list[str] = []

        class RecordingEnv(FakeEnvironment):
            def exec(self, command, phase="agent", timeout=None, as_user=None, env=None):
                return ExecResult(
                    command=command, exit_code=0, stdout="", stderr="",
                    duration_s=0.0, phase=phase,
                )

        class RecordingAgent(MinimalInstalledAgent):
            VERSION_COMMAND = None

            def _run_command(self, instr, env):
                seen.append(instr)
                return "echo ok"

        agent = RecordingAgent()
        result = agent.run(instruction, RecordingEnv(Path(".")), timeout=10)
        return result, (seen[0] if seen else "")

    def test_short_instruction_passes_unchanged(self):
        instr = "Write a hello world program."
        _, seen = self._run_with_instruction(instr)
        assert seen == instr

    def test_long_instruction_is_truncated(self):
        instr = "x" * (MAX_INSTRUCTION_CHARS + 5000)
        _, seen = self._run_with_instruction(instr)
        assert len(seen) <= MAX_INSTRUCTION_CHARS + len("\n… (truncated)")
        assert seen.endswith("… (truncated)")

    def test_exactly_at_limit_passes_unchanged(self):
        instr = "y" * MAX_INSTRUCTION_CHARS
        _, seen = self._run_with_instruction(instr)
        assert seen == instr

    def test_truncation_does_not_fail_the_trial(self):
        instr = "z" * (MAX_INSTRUCTION_CHARS * 2)
        result, _ = self._run_with_instruction(instr)
        assert result.error is None
