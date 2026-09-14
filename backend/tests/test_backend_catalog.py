"""Offline + optional API tests for Daytona backend (no sandbox create in unit tests)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from app.cli.main import app
from app.services.backend_catalog import preflight_backend
from app.services.daytona_environment import DaytonaEnvironment
from app.services.environment import make_environment


class DaytonaEnvironmentTests(unittest.TestCase):
    def test_make_environment_resolves_daytona(self) -> None:
        env = make_environment("daytona", Path("/tmp/task"))
        self.assertIsInstance(env, DaytonaEnvironment)

    @patch("app.services.daytona_environment._require_daytona_sdk")
    def test_setup_waits_for_start_without_local_docker(self, mock_sdk) -> None:
        CreateSandboxFromImageParams = MagicMock()
        Daytona = MagicMock()
        DaytonaConfig = MagicMock()
        Resources = MagicMock()
        Image = MagicMock()
        mock_sdk.return_value = (
            CreateSandboxFromImageParams,
            Daytona,
            DaytonaConfig,
            Resources,
            Image,
        )

        sandbox = MagicMock()
        sandbox.process.exec.return_value = MagicMock(
            exit_code=0, result="", artifacts=MagicMock(stdout=""), additional_properties={}
        )
        client = MagicMock()
        client.create.return_value = sandbox
        Daytona.return_value = client

        task = (
            Path(__file__).resolve().parents[2]
            / "examples"
            / "agentic-suite"
            / "01-fix-log-analyzer"
        )
        env = DaytonaEnvironment(task_dir=task, network_mode="public")
        # Clients now come from the shared cache in daytona_client, so patching
        # the SDK tuple alone would let the real Daytona class through. Snapshots
        # are disabled here so this stays a test of sandbox creation.
        env._snapshot_disabled = True
        with (
            patch("app.services.daytona_environment._env_value", return_value="test-key"),
            patch("app.services.daytona_client.get_client", return_value=client),
        ):
            env.setup()

        client.create.assert_called_once()
        sandbox.wait_for_sandbox_start.assert_called_once()
        sandbox.process.exec.assert_called()

    def test_execution_metadata_after_setup(self) -> None:
        env = DaytonaEnvironment(task_dir=Path("/tmp/t"), network_mode="public")
        sandbox = MagicMock()
        sandbox.id = "sb-abc123"
        sandbox.name = "trial-box"
        sandbox.target = "us"
        sandbox.state = "started"
        sandbox.cpu = 1
        sandbox.memory = 1
        sandbox.disk = 3
        sandbox.network_block_all = False
        sandbox.created_at = "2026-08-11T12:00:00Z"
        env._sandbox = sandbox
        meta = env.execution_metadata()
        self.assertEqual(meta["sandbox_id"], "sb-abc123")
        self.assertEqual(meta["target"], "us")
        self.assertEqual(meta["provider"], "daytona")

    @patch("app.services.daytona_environment._require_daytona_sdk")
    def test_teardown_deletes_sandbox(self, mock_sdk) -> None:
        mock_sdk.return_value = (MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        env = DaytonaEnvironment(task_dir=Path("/tmp/t"))
        sandbox = MagicMock()
        env._sandbox = sandbox
        env.teardown()
        sandbox.delete.assert_called_once()
        self.assertIsNone(env._sandbox)


class DaytonaPreflightTests(unittest.TestCase):
    def test_not_ready_without_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = preflight_backend("daytona")
        self.assertFalse(result.ready)

    @patch("app.services.daytona_environment.ping_daytona_api", return_value=(True, "ok"))
    def test_ready_when_configured(self, _ping) -> None:
        with patch.dict(os.environ, {"DAYTONA_API_KEY": "test-key"}, clear=True):
            with patch("app.services.backend_catalog._module_installed", return_value=True):
                result = preflight_backend("daytona")
        self.assertTrue(result.ready)


class BackendsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_backends_list_includes_daytona(self) -> None:
        result = self.runner.invoke(app, ["backends", "list"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("daytona", result.stdout)


if __name__ == "__main__":
    unittest.main()
