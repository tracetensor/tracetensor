"""Tests for local preflight tier assessment."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.schemas.task import EnvironmentConfig, TaskConfig
from app.services.local_preflight import LocalRunTier, assess_local_run


def _cfg(**env_kw) -> TaskConfig:
    return TaskConfig(name="test-task", environment=EnvironmentConfig(**env_kw))


def test_compose_marks_unsupported(tmp_path: Path) -> None:
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    (env_dir / "docker-compose.yaml").write_text("services: {}\n")
    (tmp_path / "task.toml").write_text('schema_version = "1.3"\n')
    tier, warnings = assess_local_run(tmp_path, _cfg())
    assert tier == LocalRunTier.UNSUPPORTED
    assert any("compose" in w.lower() for w in warnings)


def test_high_memory_marks_heavy(tmp_path: Path) -> None:
    with patch("app.services.local_preflight._docker_memory_mb", return_value=4096):
        tier, warnings = assess_local_run(tmp_path, _cfg(memory_mb=8192))
    assert tier == LocalRunTier.HEAVY
    assert any("8192" in w for w in warnings)


@patch("app.services.local_preflight.host_is_arm64", return_value=True)
def test_arm64_defaults_heavy(_arm64: object, tmp_path: Path) -> None:
    tier, warnings = assess_local_run(tmp_path, _cfg())
    assert tier == LocalRunTier.HEAVY
    assert any("Apple Silicon" in w for w in warnings)


def test_heavy_docker_image_hint(tmp_path: Path) -> None:
    tier, warnings = assess_local_run(
        tmp_path,
        _cfg(docker_image="orcabench/sre-otel-snapshot:data-0418-harbor-template"),
    )
    assert tier == LocalRunTier.HEAVY
    assert any("heavy" in w.lower() or "minutes" in w.lower() for w in warnings)
