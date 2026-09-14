"""Tests for registry cache helpers."""

from __future__ import annotations

from pathlib import Path

from app.services.registry_cache import is_cached, task_export_dir


def test_is_cached_requires_nonempty_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not is_cached(empty)
    (empty / "task.toml").write_text("x")
    assert is_cached(empty)


def test_task_export_dir() -> None:
    assert task_export_dir(Path("tasks"), "abc123") == Path("tasks/abc123")
