"""Capability declarations and the pre-provision check that reads them.

The point of this layer is that an unrunnable task is refused *before* a sandbox
exists. So the assertions are about which task shapes get refused on which
backend, and about the declarations staying honest — a backend claiming
something it cannot do is the exact failure this was built to prevent.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.schemas.task import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig
from app.services.backend_capabilities import (
    BackendCapabilities,
    capabilities_for,
    unsupported_features,
)


def _cfg(
    *,
    baseline: str = "public",
    agent_net: str | None = None,
    verifier_net: str | None = None,
    verifier_mode: str = "shared",
) -> TaskConfig:
    return TaskConfig(
        name="t/x",
        environment=EnvironmentConfig(network_mode=baseline),
        agent=AgentConfig(network_mode=agent_net),
        verifier=VerifierConfig(network_mode=verifier_net, environment_mode=verifier_mode),
    )


class DefaultsTests(unittest.TestCase):
    def test_a_backend_claims_nothing_by_default(self) -> None:
        """Forgetting to declare must mean 'refused with a message', never
        'accepted and silently run with a weaker policy than the task asked'."""
        caps = BackendCapabilities()
        self.assertFalse(
            any(
                (
                    caps.network_isolation,
                    caps.network_allowlist,
                    caps.dynamic_network,
                    caps.separate_verifier,
                )
            )
        )

    def test_planned_cloud_stubs_claim_nothing(self) -> None:
        for backend in ("modal", "e2b", "runloop", "novita"):
            caps = capabilities_for(backend)
            self.assertFalse(caps.network_isolation, backend)
            self.assertFalse(caps.separate_verifier, backend)

    def test_unknown_backend_claims_nothing(self) -> None:
        caps = capabilities_for("does-not-exist")
        self.assertFalse(caps.dynamic_network)


class DockerRulesTests(unittest.TestCase):
    def test_a_plain_task_is_accepted(self) -> None:
        self.assertEqual(unsupported_features("docker", _cfg()), [])

    def test_no_network_is_accepted(self) -> None:
        self.assertEqual(unsupported_features("docker", _cfg(baseline="no-network")), [])

    def test_phase_override_is_accepted(self) -> None:
        cfg = _cfg(baseline="no-network", agent_net="public")
        self.assertEqual(unsupported_features("docker", cfg), [])

    def test_separate_verifier_is_accepted(self) -> None:
        self.assertEqual(unsupported_features("docker", _cfg(verifier_mode="separate")), [])

    def test_allowlist_is_refused_and_says_what_to_use(self) -> None:
        problems = unsupported_features("docker", _cfg(baseline="allowlist"))
        self.assertEqual(len(problems), 1)
        self.assertIn("allowlist", problems[0])
        self.assertIn("no-network", problems[0], "the message should name a working alternative")


class RefusalMessageTests(unittest.TestCase):
    """Messages are read by someone whose run was just refused; they have to name
    the setting, the backend and the way out."""

    def _caps(self, **kw):
        return patch(
            "app.services.backend_capabilities.capabilities_for",
            return_value=BackendCapabilities(**kw),
        )

    def test_phase_override_refusal_names_the_phase_and_the_baseline(self) -> None:
        with self._caps(network_isolation=True):
            problems = unsupported_features(
                "daytona", _cfg(baseline="no-network", agent_net="public")
            )
        self.assertEqual(len(problems), 1)
        self.assertIn("[agent]", problems[0])
        self.assertIn("no-network", problems[0])
        self.assertIn("daytona", problems[0])

    def test_verifier_phase_override_is_reported_too(self) -> None:
        with self._caps(network_isolation=True):
            problems = unsupported_features(
                "daytona", _cfg(baseline="public", verifier_net="no-network")
            )
        self.assertEqual(len(problems), 1)
        self.assertIn("[verifier]", problems[0])

    def test_a_phase_matching_the_baseline_is_not_an_override(self) -> None:
        with self._caps(network_isolation=True):
            problems = unsupported_features(
                "daytona", _cfg(baseline="public", agent_net="public")
            )
        self.assertEqual(problems, [])

    def test_separate_verifier_refusal(self) -> None:
        with self._caps(network_isolation=True, dynamic_network=True):
            problems = unsupported_features("x", _cfg(verifier_mode="separate"))
        self.assertEqual(len(problems), 1)
        self.assertIn("separate", problems[0])

    def test_no_network_refusal_explains_the_risk(self) -> None:
        with self._caps():
            problems = unsupported_features("x", _cfg(baseline="no-network"))
        self.assertIn("more access than the task allows", problems[0])

    def test_every_problem_is_reported_not_just_the_first(self) -> None:
        with self._caps():
            problems = unsupported_features(
                "x", _cfg(baseline="no-network", agent_net="public", verifier_mode="separate")
            )
        self.assertEqual(len(problems), 3, problems)


class DaytonaDeclarationTests(unittest.TestCase):
    def test_dynamic_network_is_off_unless_explicitly_enabled(self) -> None:
        """Probed against a real account, both switch directions were refused:
        'Network access is restricted and cannot be overridden at the sandbox
        level'. Default off so the task is refused in milliseconds, not after a
        sandbox has been paid for."""
        from app.services.daytona_environment import DaytonaEnvironment

        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("TRACETENSOR_DAYTONA_DYNAMIC_NETWORK", None)
            self.assertFalse(DaytonaEnvironment.capabilities().dynamic_network)

    def test_dynamic_network_can_be_opted_into(self) -> None:
        from app.services.daytona_environment import DaytonaEnvironment

        with patch.dict("os.environ", {"TRACETENSOR_DAYTONA_DYNAMIC_NETWORK": "1"}):
            self.assertTrue(DaytonaEnvironment.capabilities().dynamic_network)

    def test_verified_daytona_claims(self) -> None:
        """Both proven by a real run on examples/artifact-handoff."""
        from app.services.daytona_environment import DaytonaEnvironment

        caps = DaytonaEnvironment.capabilities()
        self.assertTrue(caps.network_isolation)
        self.assertTrue(caps.separate_verifier)
        self.assertFalse(caps.network_allowlist, "allowlist enforcement is unverified")


class DockerNetworkSwitchTests(unittest.TestCase):
    def test_loosening_detaches_the_none_network_first(self) -> None:
        """A container started `--network none` cannot be connected to bridge
        while that attachment stands — the daemon refuses outright. Detaching
        `none` first is what makes no-network -> public possible at all."""
        from app.services.environment import DockerEnvironment

        env = DockerEnvironment(__import__("pathlib").Path("/tmp/t"), network_mode="no-network")
        env.container_id = "cid1"
        with patch.object(
            env, "_run", return_value=MagicMock(returncode=0, stdout="", stderr="")
        ) as run:
            env.set_network("public")

        cmds = [c.args[0] for c in run.call_args_list]
        disconnect_none = next(i for i, c in enumerate(cmds) if c[1:4] == ["network", "disconnect", "none"])
        connect_bridge = next(i for i, c in enumerate(cmds) if c[1:4] == ["network", "connect", "bridge"])
        self.assertLess(disconnect_none, connect_bridge)
        self.assertEqual(env.network_mode, "public")

    def test_tightening_disconnects_bridge(self) -> None:
        from app.services.environment import DockerEnvironment

        env = DockerEnvironment(__import__("pathlib").Path("/tmp/t"), network_mode="public")
        env.container_id = "cid1"
        with patch.object(
            env, "_run", return_value=MagicMock(returncode=0, stdout="", stderr="")
        ) as run:
            env.set_network("no-network")
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertTrue(any(c[1:4] == ["network", "disconnect", "bridge"] for c in cmds))
        self.assertEqual(env.network_mode, "no-network")


if __name__ == "__main__":
    unittest.main()
