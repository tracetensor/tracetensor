"""
Dataset manifest parsing + bundle extraction.

A dataset BUNDLE is a zip whose root holds a `dataset.toml` manifest plus one
subfolder per task (each subfolder is a standard task directory):

    dataset.toml
    sort-csv/         (instruction.md, task.toml, environment/, tests/, ...)
    log-analyzer/
    ...

The manifest is a small table-of-contents:

    name = "csv-suite"
    version = "1.0"
    tasks = ["sort-csv", "log-analyzer"]     # subfolder names, in run order
    description = "..."                        # optional
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

# Bundles hold many tasks, so allow more than a single-task upload.
_BUNDLE_MAX_FILES = 5000
_BUNDLE_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB uncompressed

MANIFEST_NAME = "dataset.toml"


class DatasetParseError(ValueError):
    """Raised when a bundle/manifest is malformed."""


@dataclass
class DatasetManifest:
    name: str
    version: str
    tasks: List[str]
    description: str = ""


@dataclass
class DatasetBundle:
    manifest: DatasetManifest
    # task subfolder name -> {relative_path: bytes} (a task dir's files)
    task_files: Dict[str, Dict[str, bytes]] = field(default_factory=dict)


def parse_manifest(content: bytes) -> DatasetManifest:
    """Parse a dataset.toml manifest into a DatasetManifest."""
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise DatasetParseError(f"dataset.toml is not valid TOML: {e}") from e

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise DatasetParseError("dataset.toml is missing a string `name`.")
    version = raw.get("version")
    if version is None:
        raise DatasetParseError("dataset.toml is missing `version`.")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, str) for t in tasks):
        raise DatasetParseError("dataset.toml `tasks` must be a non-empty list of strings.")
    if len(set(tasks)) != len(tasks):
        raise DatasetParseError("dataset.toml `tasks` contains duplicates.")

    return DatasetManifest(
        name=name,
        version=str(version),
        tasks=list(tasks),
        description=str(raw.get("description", "")),
    )


def compute_content_hash(task_files: Dict[str, Dict[str, bytes]]) -> str:
    """Stable SHA-256 over every task's files, independent of dict/zip ordering.

    Two bundles with identical task contents hash the same, so re-uploading the
    same (name, version) is idempotent while changed content is detectable.
    """
    h = hashlib.sha256()
    for task_name in sorted(task_files):
        h.update(b"\x00T:")
        h.update(task_name.encode("utf-8"))
        files = task_files[task_name]
        for rel in sorted(files):
            h.update(b"\x00F:")
            h.update(rel.encode("utf-8"))
            h.update(b"\x00=")
            h.update(hashlib.sha256(files[rel]).digest())
    return h.hexdigest()


def extract_bundle(zip_bytes: bytes) -> DatasetBundle:
    """Extract a dataset bundle zip into its manifest + per-task file maps.

    Handles a single wrapping top-level folder (common when zipping a directory)
    the same way task ingestion does. Raises DatasetParseError on any problem.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise DatasetParseError(f"not a valid zip: {e}") from e

    with zf:
        members = [m for m in zf.namelist() if not m.endswith("/")]
        if len(members) > _BUNDLE_MAX_FILES:
            raise DatasetParseError(f"bundle has too many files (>{_BUNDLE_MAX_FILES}).")
        total = sum(zf.getinfo(m).file_size for m in members)
        if total > _BUNDLE_MAX_TOTAL_BYTES:
            raise DatasetParseError("bundle is too large uncompressed (>200 MB).")

        # Strip a single common wrapping folder if every member is under it.
        tops = {m.split("/", 1)[0] for m in members if "/" in m}
        strip = None
        if len(tops) == 1:
            only = next(iter(tops))
            if (
                all(m.startswith(only + "/") for m in members)
                and (only + "/" + MANIFEST_NAME) in members
            ):
                strip = only + "/"

        def rel(m: str) -> str:
            r = m[len(strip) :] if strip else m
            return r

        # Find + parse the manifest.
        manifest_bytes = None
        for m in members:
            if rel(m) == MANIFEST_NAME:
                manifest_bytes = zf.read(m)
                break
        if manifest_bytes is None:
            raise DatasetParseError(f"bundle has no {MANIFEST_NAME} at its root.")
        manifest = parse_manifest(manifest_bytes)

        # Collect each declared task's files (everything under <task>/).
        task_files: Dict[str, Dict[str, bytes]] = {t: {} for t in manifest.tasks}
        for m in members:
            r = rel(m)
            if r == MANIFEST_NAME or not r or r.startswith("__MACOSX"):
                continue
            top, _, sub = r.partition("/")
            if top in task_files and sub:
                task_files[top][sub] = zf.read(m)

        missing = [t for t in manifest.tasks if not task_files[t]]
        if missing:
            raise DatasetParseError(
                f"manifest lists task folder(s) not present in the bundle: {missing}"
            )

    return DatasetBundle(manifest=manifest, task_files=task_files)
