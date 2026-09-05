"""Agent version capture, Claude Code's event stream, and failure classification.

These three exist so a stored result can be interpreted later: what version
produced this score, what did the agent actually do, and was this failure worth
retrying. Each was previously either missing or decided by substring matching.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.models.enums import TrialStatus
from app.services.agents.claude_code import ClaudeCodeAgent, _parse_stream_json
from app.services.failure_classifier import FailureKind, classify, is_retryable_kind
from app.services.infra_retry import is_infra_retryable


def _event(**kw) -> str:
    return json.dumps(kw)


def _assistant(*blocks) -> str:
    return _event(type="assistant", message={"content": list(blocks)})


def _user(*blocks) -> str:
    return _event(type="user", message={"content": list(blocks)})


class VersionParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ClaudeCodeAgent("haiku")

    def test_reads_a_version_out_of_varied_formats(self) -> None:
        cases = {
            "1.0.18 (Claude Code)": "1.0.18",
            "claude v1.2.3": "1.2.3",
            "2.1.233": "2.1.233",
            "codex-cli 0.4.2\n": "0.4.2",
            "  1.5 \n": "1.5",
        }
        for stdout, expected in cases.items():
            self.assertEqual(self.agent.parse_version(stdout), expected, stdout)

    def test_unrecognised_output_is_kept_rather_than_dropped(self) -> None:
        self.assertEqual(self.agent.parse_version("nightly-build\nextra"), "nightly-build")

    def test_empty_output_is_none(self) -> None:
        self.assertIsNone(self.agent.parse_version(""))
        self.assertIsNone(self.agent.parse_version("   \n "))

    def test_capture_returns_none_when_the_probe_fails(self) -> None:
        """A version we cannot read must never fail the trial."""
        env = MagicMock()
        env.exec.return_value = MagicMock(exit_code=127, stdout="", stderr="not found")
        self.assertIsNone(self.agent._capture_version(env))

    def test_capture_survives_an_exec_that_raises(self) -> None:
        env = MagicMock()
        env.exec.side_effect = RuntimeError("sandbox gone")
        self.assertIsNone(self.agent._capture_version(env))

    def test_agent_without_a_version_command_probes_nothing(self) -> None:
        env = MagicMock()
        with patch.object(type(self.agent), "VERSION_COMMAND", None):
            self.assertIsNone(self.agent._capture_version(env))
        env.exec.assert_not_called()


class StreamJsonTests(unittest.TestCase):
    def test_tool_calls_become_steps_paired_with_their_results(self) -> None:
        stdout = "\n".join(
            [
                _event(type="system", subtype="init"),
                _assistant(
                    {"type": "text", "text": "Let me look."},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/app/x.py"}},
                ),
                _user({"type": "tool_result", "tool_use_id": "t1", "content": "1\tdef add()"}),
                _event(type="result", subtype="success", total_cost_usd=0.01, num_turns=2),
            ]
        )
        rec = _parse_stream_json(stdout)
        self.assertEqual(rec["format"], "stream-json")
        self.assertEqual(rec["tool_calls"], ["Read"])
        tool_step = next(s for s in rec["steps"] if s["kind"] == "tool_use")
        self.assertEqual(tool_step["tool"], "Read")
        self.assertIn("def add()", tool_step["output"], "result was not attached to its call")
        self.assertFalse(tool_step["is_error"])

    def test_result_event_is_kept_for_costing(self) -> None:
        stdout = _event(type="result", subtype="success", total_cost_usd=0.25, num_turns=3)
        rec = _parse_stream_json(stdout)
        self.assertEqual(rec["result"]["total_cost_usd"], 0.25)

    def test_cost_is_read_through_the_stream_wrapper(self) -> None:
        agent = ClaudeCodeAgent("haiku")
        raw = {"format": "stream-json", "result": {"total_cost_usd": 0.5, "num_turns": 4}}
        calls = agent._llm_calls(raw)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cost_usd"], 0.5)
        self.assertEqual(calls[0]["api_calls"], 4)

    def test_legacy_single_object_output_still_prices(self) -> None:
        """A trajectory recorded before the switch must still be readable."""
        agent = ClaudeCodeAgent("haiku")
        calls = agent._llm_calls({"total_cost_usd": 0.3, "num_turns": 2})
        self.assertEqual(calls[0]["cost_usd"], 0.3)

    def test_malformed_lines_are_skipped_not_fatal(self) -> None:
        stdout = "\n".join(
            [
                "warning: something on stdout",
                "{not json at all",
                _assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}}),
            ]
        )
        rec = _parse_stream_json(stdout)
        self.assertEqual(rec["tool_calls"], ["Bash"])

    def test_output_that_is_not_a_stream_falls_back(self) -> None:
        rec = _parse_stream_json('{"total_cost_usd": 0.02, "num_turns": 1}')
        self.assertEqual(rec.get("total_cost_usd"), 0.02)

    def test_empty_output_is_none(self) -> None:
        self.assertIsNone(_parse_stream_json(""))
        self.assertIsNone(_parse_stream_json(None))

    def test_an_errored_tool_result_is_flagged(self) -> None:
        stdout = "\n".join(
            [
                _assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
                _user({"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}),
            ]
        )
        rec = _parse_stream_json(stdout)
        self.assertTrue(rec["steps"][0]["is_error"])

    def test_the_run_command_asks_for_a_stream(self) -> None:
        agent = ClaudeCodeAgent("haiku")
        cmd = agent._run_command("do it", MagicMock())
        self.assertIn("--output-format stream-json", cmd)
        self.assertIn("--verbose", cmd, "stream-json needs --verbose in print mode")


class FailureClassificationTests(unittest.TestCase):
    def test_kinds(self) -> None:
        cases = {
            "Room setup failed: docker pull I/O error": FailureKind.INFRA,
            "docker pull failed: no matching manifest": FailureKind.INFRA,
            "rate limit exceeded, please retry": FailureKind.RATE_LIMIT,
            "429 Too Many Requests": FailureKind.RATE_LIMIT,
            "context window exceeded for this request": FailureKind.CONTEXT_WINDOW,
            "agent gave up": FailureKind.UNKNOWN,
            "": FailureKind.UNKNOWN,
        }
        for text, expected in cases.items():
            self.assertEqual(classify(text), expected, text)

    def test_permanent_kinds_win_over_the_infra_vocabulary_they_share(self) -> None:
        """The old substring scan retried anything containing 'docker'. A bad
        credential says 'docker' and fails identically every time."""
        self.assertEqual(classify("docker login failed: unauthorized"), FailureKind.AUTH)
        self.assertEqual(classify("claude-code needs ANTHROPIC_API_KEY configured."), FailureKind.AUTH)
        self.assertEqual(
            classify(
                "Failed to update network settings: Network access is restricted "
                "and cannot be overridden at the sandbox level"
            ),
            FailureKind.CONFIG,
        )

    def test_retry_policy_follows_the_kind(self) -> None:
        for kind in (FailureKind.INFRA, FailureKind.RATE_LIMIT, FailureKind.TIMEOUT):
            self.assertTrue(is_retryable_kind(kind), kind)
        for kind in (
            FailureKind.AUTH,
            FailureKind.CONFIG,
            FailureKind.CONTEXT_WINDOW,
            FailureKind.AGENT,
            FailureKind.UNKNOWN,
        ):
            self.assertFalse(is_retryable_kind(kind), kind)

    def test_spent_work_blocks_retry_regardless_of_kind(self) -> None:
        """Even a clean infra failure is not re-run once the agent has started —
        that would be a second paid attempt, not a recovery."""
        outcome = MagicMock(
            status=TrialStatus.ERROR.value,
            error="docker died",
            trajectory={"llm_calls": [{"model": "haiku"}]},
        )
        self.assertFalse(is_infra_retryable(outcome))

    def test_auth_failure_is_no_longer_retried(self) -> None:
        outcome = MagicMock(
            status=TrialStatus.ERROR.value,
            error="docker login failed: unauthorized",
            trajectory={},
        )
        self.assertFalse(is_infra_retryable(outcome))

    def test_rate_limit_is_now_retried(self) -> None:
        """It wasn't, before: no marker in the old list matched a 429."""
        outcome = MagicMock(
            status=TrialStatus.ERROR.value, error="429 rate limit", trajectory={}
        )
        self.assertTrue(is_infra_retryable(outcome))


class VersionCommandDeclarationTests(unittest.TestCase):
    def test_installed_agents_declare_a_version_command(self) -> None:
        from app.services.agents.codex import CodexAgent
        from app.services.agents.langgraph_agent import LangGraphAgent
        from app.services.agents.mini_swe import MiniSweAgent

        for agent in (
            ClaudeCodeAgent("haiku"),
            CodexAgent(None),
            MiniSweAgent(None),
            LangGraphAgent(str(Path("/tmp")), "anthropic/claude-haiku-4-5"),
        ):
            self.assertTrue(agent.VERSION_COMMAND, type(agent).__name__)


if __name__ == "__main__":
    unittest.main()
