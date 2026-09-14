"""pytest: DockerEnvironment passes --platform to build/pull/run."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.environment import DockerEnvironment


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    (env_dir / "Dockerfile").write_text("FROM python:3.11-slim\n")
    return tmp_path


def test_build_passes_platform_to_docker_build(task_dir: Path) -> None:
    env = DockerEnvironment(task_dir, platform="linux/amd64")
    with patch.object(env, "_run", return_value=MagicMock(returncode=0)) as run:
        env.build()
    args = run.call_args[0][0]
    assert "build" in args
    assert "--platform" in args
    assert "linux/amd64" in args


def test_docker_image_triggers_pull_with_platform(task_dir: Path) -> None:
    env = DockerEnvironment(
        task_dir,
        docker_image="orcabench/sre-otel-snapshot:tag",
        platform="linux/amd64",
    )
    with patch.object(env, "_run", return_value=MagicMock(returncode=0)) as run:
        env.build()
    assert run.call_count == 1
    args = run.call_args[0][0]
    assert args[:2] == ["docker", "pull"]
    assert "--platform" in args
    assert "linux/amd64" in args
    assert "orcabench/sre-otel-snapshot:tag" in args


def test_docker_pull_failure_surfaces_platform_hint(task_dir: Path) -> None:
    env = DockerEnvironment(task_dir, docker_image="bad/image:tag", platform=None)
    fail = MagicMock(returncode=1, stderr="no matching manifest for linux/arm64/v8", stdout="")
    with patch.object(env, "_run", return_value=fail):
        with pytest.raises(RuntimeError, match="amd64-only"):
            env.build()


def test_setup_passes_platform_to_docker_run(task_dir: Path) -> None:
    env = DockerEnvironment(task_dir, platform="linux/amd64")
    ok = MagicMock(returncode=0, stdout="cid123\n", stderr="")
    with patch.object(env, "_run", return_value=ok) as run:
        env.setup()
    run_args = [c[0][0] for c in run.call_args_list]
    run_cmd = next(a for a in run_args if a[1] == "run")
    assert "--platform" in run_cmd
    assert "linux/amd64" in run_cmd
