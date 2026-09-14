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


class TestMultiStep:
    """Multi-step tasks: shared sandbox, per-step agent+verify, mean reward."""

    def _make_multi_step_task(self, tmp_path):
        """Create a two-step task directory with steps/ auto-detection layout."""
        d = tmp_path / "multi-step-task"
        (d / "environment").mkdir(parents=True)
        (d / "environment" / "Dockerfile").write_text("FROM busybox")
        (d / "task.toml").write_text(
            'schema_version = "1.3"\n\n'
            '[task]\nname = "test/multi-step"\n\n'
            '[environment]\nnetwork_mode = "no-network"\n'
        )
        for step, (instr, reward) in enumerate(
            [("Step 1: write hello.txt", "0.5"), ("Step 2: write world.txt", "1.0")], 1
        ):
            sd = d / "steps" / f"0{step}-step"
            (sd / "tests").mkdir(parents=True)
            (sd / "instruction.md").write_text(instr)
            # test.sh emits REWARD=<value> so the verifier scores it
            (sd / "tests" / "test.sh").write_text(
                f"#!/bin/sh\necho REWARD={reward}\nexit 0\n"
            )
        return d

    def test_multi_step_mean_reward(self, tmp_path):
        """Final reward is mean of per-step rewards: both steps score 0.75 → mean 0.75."""
        task_dir = self._make_multi_step_task(tmp_path)
        env = FakeEnvironment(task_dir)
        # Verifier reads /logs/verifier/reward.txt; plant 0.75 for each step.
        env.files["/logs/verifier/reward.txt"] = "0.75"
        outcome, _ = run(task_dir, env=env)
        assert outcome.status == "completed"
        assert outcome.reward == 0.75
        # The trajectory carries per-step rewards
        assert "step_rewards" in outcome.trajectory
        assert outcome.trajectory["step_rewards"] == [0.75, 0.75]

    def test_multi_step_all_must_pass_for_passed(self, tmp_path):
        """passed=True only when all steps pass; a 0.0 step makes passed=False."""
        task_dir = self._make_multi_step_task(tmp_path)
        env = FakeEnvironment(task_dir)
        # reward.txt = 0.0 → both steps fail the default 0.5 pass_threshold
        env.files["/logs/verifier/reward.txt"] = "0.0"
        outcome, _ = run(task_dir, env=env)
        assert not outcome.passed

    def test_single_step_path_unaffected(self, task_dir):
        """A task without steps/ still runs the original single-step path."""
        outcome, _ = run(task_dir)
        assert outcome.status == "completed"
        assert "step_rewards" not in outcome.trajectory


class TestSchemaVersion:
    """B — every trajectory carries tt_schema_version so consumers can detect breakage."""

    def test_single_step_trajectory_has_schema_version(self, task_dir):
        outcome, _ = run(task_dir)
        assert outcome.trajectory.get("tt_schema_version") == "1.0"

    def test_multi_step_trajectory_has_schema_version(self, tmp_path):
        d = tmp_path / "ms"
        (d / "environment").mkdir(parents=True)
        (d / "environment" / "Dockerfile").write_text("FROM busybox")
        (d / "task.toml").write_text(
            'schema_version = "1.3"\n[task]\nname = "test/ms"\n[environment]\nnetwork_mode = "no-network"\n'
        )
        for i, name in enumerate(["01-a", "02-b"], 1):
            sd = d / "steps" / name
            (sd / "tests").mkdir(parents=True)
            (sd / "instruction.md").write_text(f"step {i}")
            (sd / "tests" / "test.sh").write_text("#!/bin/sh\nexit 0\n")
        env = FakeEnvironment(d)
        env.files["/logs/verifier/reward.txt"] = "1.0"
        outcome, _ = run(d, env=env)
        assert outcome.trajectory.get("tt_schema_version") == "1.0"
