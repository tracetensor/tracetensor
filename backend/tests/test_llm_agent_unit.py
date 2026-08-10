"""
LLMAgent's loop, tested without a model API.

Everything here goes through the `llm_fn` seam. The behaviors below are the ones
that actually decide whether a run is trustworthy — did the agent do any work,
did a guardrail fire, did the budget hold — and until the seam existed none of
them had a test, because each one needed a paid call to observe.
"""

from __future__ import annotations

import pytest
from conftest import FakeEnvironment, ScriptedLLM

from app.services.agents.llm_agent import LLMAgent
from app.services.llm import ProviderError


def agent(replies, **kw):
    return LLMAgent(provider="anthropic", model="test-model", llm_fn=ScriptedLLM(replies), **kw)


class TestCommandLoop:
    def test_runs_commands_until_done(self):
        env = FakeEnvironment()
        a = agent(["ls -la", "cat file.txt", "DONE"])
        result = a.run("do it", env)

        assert env.commands == ["ls -la", "cat file.txt"]
        assert result.error is None
        assert len(result.steps) == 2

    def test_stops_at_the_step_budget(self):
        """A model that never says DONE must not loop forever — the budget is
        what stands between a stuck agent and an unbounded bill."""
        env = FakeEnvironment()
        a = LLMAgent(
            provider="anthropic",
            model="m",
            max_steps=3,
            llm_fn=ScriptedLLM(["echo 1"] * 50),
        )
        a.run("do it", env)
        assert len(env.commands) == 3

    def test_feeds_command_output_back_to_the_model(self):
        env = FakeEnvironment(responses={"pytest": {"exit_code": 1, "stdout": "2 failed"}})
        scripted = ScriptedLLM(["pytest", "DONE"])
        LLMAgent(provider="anthropic", model="m", llm_fn=scripted).run("fix tests", env)

        # The second call must show the model what the first command did.
        second = scripted.calls[1]["history"]
        assert "[exit 1]" in second
        assert "2 failed" in second

    def test_prose_is_passed_to_the_shell_and_its_error_fed_back(self):
        """Deliberate: the loop doesn't try to classify prose. The shell rejects
        it, and that rejection goes back to the model, which is a better signal
        than us guessing what counts as a command."""
        env = FakeEnvironment(responses={"consider": {"exit_code": 127, "stderr": "not found"}})
        scripted = ScriptedLLM(["I think we should consider the options.", "ls", "DONE"])
        LLMAgent(provider="anthropic", model="m", llm_fn=scripted).run("go", env)
        assert len(env.commands) == 2
        assert "[exit 127]" in scripted.calls[1]["history"]

    def test_an_empty_code_fence_is_skipped_without_running_anything(self):
        """`_clean_command` yields nothing here, so the loop must re-prompt rather
        than exec an empty string."""
        env = FakeEnvironment()
        scripted = ScriptedLLM(["```\n```", "ls", "DONE"])
        LLMAgent(provider="anthropic", model="m", llm_fn=scripted).run("go", env)
        assert env.commands == ["ls"]
        assert "No runnable command" in scripted.calls[1]["history"]


class TestSilentFailureDetection:
    """A trial that scores 0.0 because the agent never ran anything must not look
    like a trial where the agent tried and failed. These are the checks that keep
    those two cases distinguishable."""

    def test_done_without_any_command_is_an_error(self):
        result = agent(["DONE"]).run("go", FakeEnvironment())
        assert result.error is not None
        assert "DONE" in result.error

    def test_empty_model_output_is_an_error(self):
        result = agent([""]).run("go", FakeEnvironment())
        assert result.error is not None

    def test_reasoning_model_empty_content_is_called_out(self):
        """Large output_tokens with no text means the model spent its budget on
        hidden reasoning — a distinct failure worth naming, since the fix is to
        change models rather than to change the task."""
        a = LLMAgent(
            provider="anthropic",
            model="m",
            llm_fn=ScriptedLLM([""], usage={"output_tokens": 900}),
        )
        result = a.run("go", FakeEnvironment())
        assert "reasoning-model" in (result.error or "")


class TestErrorHandling:
    def test_provider_error_is_returned_not_raised(self):
        """An agent failure is data. Raising here would turn a bad API key into a
        crashed trial instead of a recorded result."""
        result = agent([ProviderError("no key configured")]).run("go", FakeEnvironment())
        assert result.error is not None
        assert "no key" in result.error
        assert result.steps == []

    def test_unexpected_sdk_error_is_also_contained(self):
        result = agent([RuntimeError("connection reset")]).run("go", FakeEnvironment())
        assert "Model call failed" in (result.error or "")

    def test_partial_work_is_preserved_when_the_model_dies(self):
        env = FakeEnvironment()
        result = agent(["ls", RuntimeError("boom")]).run("go", env)
        assert len(result.steps) == 1  # the successful command is not lost
        assert result.error is not None


class TestBudgets:
    def test_oversized_instruction_is_truncated_before_the_prompt(self):
        """instruction.md is task-author content. An unbounded one is both a cost
        vector and a bigger prompt-injection surface."""
        from app.services.agents.base import MAX_INSTRUCTION_CHARS

        scripted = ScriptedLLM(["DONE"])
        LLMAgent(provider="anthropic", model="m", llm_fn=scripted).run(
            "x" * (MAX_INSTRUCTION_CHARS * 2), FakeEnvironment()
        )
        sent = scripted.calls[0]["history"]
        assert len(sent) < MAX_INSTRUCTION_CHARS * 2
        assert "truncated" in sent

    def test_zero_timeout_stops_before_any_call(self):
        env = FakeEnvironment()
        result = agent(["ls", "DONE"]).run("go", env, timeout=-1)
        assert env.commands == []
        assert "timed out" in (result.error or "")

    def test_usage_is_recorded_per_call(self):
        result = agent(["ls", "DONE"]).run("go", FakeEnvironment())
        assert len(result.llm_calls) == 2
        assert all(c["input_tokens"] == 10 for c in result.llm_calls)


class TestGuardrails:
    def test_credential_access_is_flagged(self):
        """The sandbox is the real defense; the guardrail makes risky behavior
        visible rather than silent."""
        env = FakeEnvironment()
        result = agent(["cat ~/.ssh/id_rsa", "DONE"]).run("go", env)
        assert result.guardrail_flags
        assert result.guardrail_flags[0]["category"] == "credential_access"

    def test_flagged_command_still_runs_in_flag_mode(self):
        env = FakeEnvironment()
        agent(["cat ~/.ssh/id_rsa", "DONE"]).run("go", env)
        assert "cat ~/.ssh/id_rsa" in env.commands


class TestConstruction:
    def test_unknown_provider_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            LLMAgent(provider="not-a-provider")

    def test_max_steps_defaults_to_the_configured_budget(self, settings_override):
        with settings_override(LLM_AGENT_MAX_STEPS=3):
            assert LLMAgent(provider="anthropic", model="m").max_steps == 3

    def test_the_real_llm_is_the_default(self):
        """The seam must not change production behavior."""
        from app.services import llm

        assert LLMAgent(provider="anthropic", model="m")._llm_fn is llm.call_llm


class TestLiteLLMModelIds:
    """The shared helpers behind every "<provider>/<model>" agent.

    These replace near-identical copies across installed agents and the
    registry's key check). The registry gate and the agent's own key lookup now
    read the same mapping — when they didn't, a provider added to one and not the
    other produced a run that passed validation and then failed in the container.
    """

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("anthropic/claude-haiku-4-5", ("anthropic/claude-haiku-4-5", "anthropic")),
            ("openai/gpt-5", ("openai/gpt-5", "openai")),
            ("claude-haiku-4-5", ("anthropic/claude-haiku-4-5", "anthropic")),  # bare → anthropic
            (None, ("anthropic/default-m", "anthropic")),  # falls back to the default
        ],
    )
    def test_normalizes_a_model_id_to_provider_and_full_id(self, given, expected):
        from app.services.agents.base import normalize_litellm_model

        assert normalize_litellm_model(given, "anthropic/default-m") == expected

    def test_returns_the_env_var_and_key_for_a_configured_provider(self, settings_override):
        from app.services.agents.base import litellm_api_key

        with settings_override(ANTHROPIC_API_KEY="sk-test"):
            assert litellm_api_key("anthropic", "mini-swe", "anthropic/m") == (
                "ANTHROPIC_API_KEY",
                "sk-test",
            )

    def test_names_the_missing_variable_when_the_key_is_absent(self, settings_override):
        """Failing with the exact variable to set beats handing the container a
        blank key and failing inside the agent's own CLI."""
        from app.services.agents.base import litellm_api_key

        with settings_override(OPENAI_API_KEY=None):
            with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
                litellm_api_key("openai", "mini-swe", "openai/gpt-5")

    def test_an_unknown_provider_still_reports_something_actionable(self, settings_override):
        from app.services.agents.base import litellm_api_key

        with pytest.raises(ProviderError, match="a model key"):
            litellm_api_key("not-a-provider", "mini-swe", "not-a-provider/m")

    def test_the_registry_gate_and_the_agent_agree(self, settings_override):
        """Same mapping, so a key the gate accepts is one the agent can read."""
        from app.core.config import settings
        from app.services.agents.base import litellm_api_key
        from app.services.agents.registry import litellm_key_check

        with settings_override(ANTHROPIC_API_KEY="sk-test"):
            assert litellm_key_check("anthropic/m", settings) is None
            assert litellm_api_key("anthropic", "mini-swe", "anthropic/m")[1] == "sk-test"

        with settings_override(ANTHROPIC_API_KEY=None):
            assert litellm_key_check("anthropic/m", settings) == "ANTHROPIC_API_KEY"
            with pytest.raises(ProviderError):
                litellm_api_key("anthropic", "mini-swe", "anthropic/m")

    def test_agents_build_their_model_id_through_the_shared_helper(self):
        """Guards the de-duplication itself: mini-swe accepts a bare id and
        normalizes it the same way."""
        from app.services.agents.mini_swe import MiniSweAgent

        assert MiniSweAgent("claude-haiku-4-5").model == "anthropic/claude-haiku-4-5"
        assert MiniSweAgent(None).provider == "anthropic"
