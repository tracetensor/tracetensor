"""
run_trial's orchestration, tested without Docker.

This is the function the whole product rests on: it decides what network the
agent gets, whether the verifier is isolated, and what a 0.0 means. Testing it
used to require a real container, which meant in practice it wasn't tested —
and that is exactly how a silently-swallowed task.toml parse survived long
enough to be found in a code review rather than by CI.

Every test drives it through the `environment_factory` / `agent_factory` seams.
"""

from __future__ import annotations

from conftest import FakeAgent, FakeEnvironment

from app.models.enums import TrialStatus
from app.services.trial_runner import run_trial


def factories(env=None, agent=None):
    """Build the two seam callables run_trial expects, over one shared env so a
    test can inspect what the trial did to it."""
    env = env or FakeEnvironment()
    agent = agent or FakeAgent(commands=["echo hi"])
    made: list = [env]

    def env_factory(backend, task_dir, **kw):
        # The isolated-verifier path asks for a SECOND environment; hand it a
        # fresh one so the two can be told apart.
        if len(made) > 1 or kw.get("dockerfile_dir") == "tests":
            extra = FakeEnvironment(task_dir, **kw)
            made.append(extra)
            return extra
        env.kwargs = kw
        return env

    def agent_factory(name, task_dir, model=None, **kw):
        # The seam is typed Callable[..., BaseAgent] — variadic on purpose, so the
        # runner can thread new per-task settings (max_steps, …) without every
        # double having to be rewritten. Recorded so tests can assert on them.
        agent.factory_kwargs = kw
        return agent

    return env_factory, agent_factory, made


def run(task_dir, **kw):
    env_factory, agent_factory, made = factories(kw.pop("env", None), kw.pop("agent", None))
    outcome = run_trial(
        task_dir,
        kw.pop("instruction", "do the thing"),
        environment_factory=env_factory,
        agent_factory=agent_factory,
        **kw,
    )
    return outcome, made


class TestSandboxContract:
    """The task's declared isolation must actually be applied — this is the part
    where a silent failure is a security problem, not just a wrong number."""

    def test_declared_network_mode_reaches_the_environment(self, task_dir):
        _outcome, made = run(task_dir)
        assert made[0].kwargs["network_mode"] == "no-network"

    def test_agent_phase_network_is_set_from_the_task(self, task_dir):
        _outcome, made = run(task_dir)
        # set_network is called before the agent works and again before the
        # verifier; both must reflect the task's declaration.
        assert made[0].networks == ["no-network", "no-network"]

    def test_a_phase_override_beats_the_baseline(self, task_dir):
        (task_dir / "task.toml").write_text(
            '[task]\nname = "t/x"\n\n'
            '[environment]\nnetwork_mode = "no-network"\n\n'
            '[agent]\nnetwork_mode = "public"\n'
        )
        _outcome, made = run(task_dir)
        assert made[0].networks[0] == "public"  # agent phase
        assert made[0].networks[1] == "no-network"  # verifier falls back to baseline

    def test_unparseable_task_toml_fails_the_trial(self, task_dir):
        """The regression test for the swallowed parse. A task declaring
        no-network with a broken file must NOT run with the public default."""
        (task_dir / "task.toml").write_text('[environment]\nnetwork_mode = = "no-network"\n')
        outcome, made = run(task_dir)

        assert outcome.status == TrialStatus.ERROR.value
        assert "task.toml" in outcome.error
        assert made[0].setup_called is False  # nothing was started at all

    def test_missing_task_toml_fails_the_trial(self, tmp_path):
        (tmp_path / "empty").mkdir()
        outcome, _ = run(tmp_path / "empty")
        assert outcome.status == TrialStatus.ERROR.value


class TestPhaseSequencing:
    def test_emits_each_phase_in_order(self, task_dir):
        events: list = []
        run(task_dir, on_event=events.append)
        phases = [(e["phase"], e["status"]) for e in events if e["type"] == "phase"]
        assert phases == [
            ("setup", "running"),
            ("setup", "done"),
            ("agent", "running"),
            ("agent", "done"),
            ("verify", "running"),
            ("verify", "done"),
            ("score", "done"),
        ]

    def test_teardown_runs_even_when_the_agent_explodes(self, task_dir):
        class Exploding(FakeAgent):
            def run(self, *a, **kw):
                raise RuntimeError("agent crashed")

        outcome, made = run(task_dir, agent=Exploding())
        assert outcome.status == TrialStatus.ERROR.value
        assert made[0].teardown_called is True

    def test_setup_failure_is_reported_not_raised(self, task_dir):
        class BadEnv(FakeEnvironment):
            def setup(self):
                raise RuntimeError("no docker daemon")

        outcome, _ = run(task_dir, env=BadEnv())
        assert outcome.status == TrialStatus.ERROR.value
        assert "Room setup failed" in outcome.error


class TestWarnings:
    """Warnings exist so a 0.0 that came from a misconfigured task doesn't look
    like a 0.0 the agent earned."""

    def test_agent_error_with_no_steps_is_surfaced(self, task_dir):
        outcome, _ = run(task_dir, agent=FakeAgent(commands=[], error="no API key"))
        assert any("no API key" in w for w in outcome.warnings)

    def test_llm_called_but_nothing_run_is_surfaced(self, task_dir):
        outcome, _ = run(
            task_dir,
            agent=FakeAgent(commands=[], llm_calls=[{"provider": "anthropic", "api_calls": 3}]),
        )
        assert any("no bash command" in w for w in outcome.warnings)

    def test_isolated_verifier_without_artifacts_warns_loudly(self, task_dir):
        """The grader sees only what's transferred. With nothing declared it
        reads an empty container and scores 0.0 — indistinguishable from a real
        failure unless we say so."""
        (task_dir / "task.toml").write_text(
            '[task]\nname = "t/x"\n\n[verifier]\nenvironment_mode = "separate"\n'
        )
        (task_dir / "tests" / "Dockerfile").write_text("FROM python:3.11-slim")
        outcome, _ = run(task_dir)
        assert any("no" in w and "artifacts" in w for w in outcome.warnings)


class TestUsageRollup:
    def test_no_llm_calls_reports_zero_not_unknown(self, task_dir):
        outcome, _ = run(task_dir)
        summary = outcome.trajectory["llm_usage_summary"]
        assert summary["calls"] == 0
        assert summary["cost_usd"] is None

    def test_partial_cost_does_not_masquerade_as_complete(self, task_dir):
        """One call with an unknown cost must make the TOTAL unknown, not zero —
        an understated bill is worse than an absent one."""
        outcome, _ = run(
            task_dir,
            agent=FakeAgent(
                commands=["echo hi"],
                llm_calls=[{"cost_usd": 0.01}, {"cost_usd": None}],
            ),
        )
        assert outcome.trajectory["llm_usage_summary"]["cost_usd"] is None

    def test_installed_agent_call_count_is_not_undercounted(self, task_dir):
        """An installed agent reports its whole run as one record carrying the
        real api_calls; collapsing that to 1 would understate usage."""
        outcome, _ = run(
            task_dir,
            agent=FakeAgent(commands=["x"], llm_calls=[{"api_calls": 12, "cost_usd": 0.5}]),
        )
        assert outcome.trajectory["llm_usage_summary"]["calls"] == 12
