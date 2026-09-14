"""Local cache for Harbor registry downloads."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def default_cache_root() -> Path:
    override = (os.getenv("TRACETENSOR_REGISTRY_CACHE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "tracetensor" / "registry"


def task_cache_dir(ref_org: str, ref_name: str, content_hash: str) -> Path:
    return default_cache_root() / "tasks" / ref_org / ref_name / content_hash


def task_export_dir(output: Path, task_name: str) -> Path:
    return output / task_name


def dataset_export_root(output: Path, dataset_short_name: str) -> Path:
    return output / dataset_short_name


def is_cached(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def ensure_empty_dir(path: Path, *, overwrite: bool) -> None:
    if path.exists():
        if not overwrite:
            return
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
