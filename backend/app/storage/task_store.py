"""
Evaluation file storage.

Layout on disk (Standard task layout):

    <TASKS_ROOT>/<safe-name>/
        instruction.md
        task.toml
        environment/Dockerfile
        solution/solve.sh
        tests/test.sh
"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path
from typing import Optional

# Files/paths we refuse to write, to keep zip extraction safe.
_ZIP_MAX_FILES = 500
_ZIP_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB uncompressed


def safe_task_name(name: str) -> str:
    """Turn 'myorg/sort-csv' into a filesystem-safe folder name."""
    return name.replace("/", "__").replace("\\", "__").strip()


def _guard_member_path(base: Path, target: Path) -> None:
    """Prevent Zip Slip: refuse any path escaping the base directory."""
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    if base_resolved not in target_resolved.parents and base_resolved != target_resolved:
        raise ValueError(f"Unsafe path in archive: {target}")


def _guard_task_dir(tasks_root: Path, task_dir: Path) -> None:
    """C-1: Prevent path traversal via task name (e.g. name='..').

    safe_task_name replaces slashes but a bare '..' or a name that still resolves
    outside the root after joining must be rejected before any rmtree or mkdir.
    """
    resolved_root = tasks_root.resolve()
    resolved_dir = task_dir.resolve()
    if resolved_root != resolved_dir and resolved_root not in resolved_dir.parents:
        raise ValueError(
            f"Task name resolves outside storage root: {task_dir.name!r} — "
            "choose a name without path components."
        )


def store_task_from_files(tasks_root: Path, task_name: str, files: dict[str, bytes]) -> Path:
    """Write a mapping of {relative_path: bytes} into a fresh evaluation folder.

    Any existing folder with the same name is replaced.
    """
    task_dir = tasks_root / safe_task_name(task_name)
    _guard_task_dir(tasks_root, task_dir)  # C-1: must be inside tasks_root
    if task_dir.exists():
        shutil.rmtree(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)

    for rel_path, content in files.items():
        dest = task_dir / rel_path
        _guard_member_path(task_dir, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    return task_dir


def extract_zip_files(zip_bytes: bytes) -> dict[str, bytes]:
    """Extract an evaluation ZIP into {relative_path: bytes}, flattening one root folder."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = [m for m in zf.namelist() if not m.endswith("/")]

        if len(members) > _ZIP_MAX_FILES:
            raise ValueError(f"Archive has too many files (>{_ZIP_MAX_FILES}).")
        total = sum(zf.getinfo(m).file_size for m in members)
        if total > _ZIP_MAX_TOTAL_BYTES:
            raise ValueError("Archive is too large when uncompressed (>50 MB).")

        tops = {m.split("/", 1)[0] for m in members if "/" in m}
        strip_prefix = None
        if len(tops) == 1:
            only = next(iter(tops))
            if all(m.startswith(only + "/") for m in members):
                strip_prefix = only + "/"

        files: dict[str, bytes] = {}
        for m in members:
            rel = m[len(strip_prefix) :] if strip_prefix else m
            if not rel or rel.startswith("__MACOSX"):
                continue
            files[rel] = zf.read(m)
    return files


def store_task_from_zip(tasks_root: Path, task_name: str, zip_bytes: bytes) -> Path:
    """Extract an uploaded ZIP of an evaluation directory into storage.

    Handles the common case where a zip wraps everything in a single top-level
    folder (e.g. sort-csv/instruction.md) by flattening that folder away.
    """
    files = extract_zip_files(zip_bytes)
    return store_task_from_files(tasks_root, task_name, files)


def read_task_file(task_dir: Path, relative_path: str) -> Optional[str]:
    """Read a stored file as text, or None if it doesn't exist."""
    p = (task_dir / relative_path).resolve()
    if not p.is_relative_to(task_dir.resolve()):
        return None
    if p.exists() and p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")
    return None


def delete_task(task_dir: Path) -> None:
    if task_dir.exists():
        shutil.rmtree(task_dir)
