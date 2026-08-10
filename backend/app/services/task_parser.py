"""
task.toml parser.

Doctor analogy: the *Intake Clerk* — reads the raw paperwork the patient
hands over (task.toml) and copies it neatly onto the structured Patient Chart
(TaskConfig).

task.toml uses these sections:

    schema_version = "1.3"
    [task]        name, description, authors, keywords
    [metadata]    category, difficulty_explanation, ... (free-form)
    [verifier]    timeout_sec, environment_mode, network_mode
    [agent]       timeout_sec, network_mode, user
    [environment] build_timeout_sec, network_mode, docker_image, os, ...

Legacy Terminal-Bench 1.0 tasks use `version = "1.0"`, flat metadata fields
(author_name, tags), and human-readable resource sizes (memory = "4G"). Those
are normalized to the 1.3 shape before validation.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

from app.schemas.task import (
    AgentConfig,
    Author,
    EnvironmentConfig,
    TaskConfig,
    VerifierConfig,
)


class TaskParseError(ValueError):
    """Raised when task.toml cannot be understood."""


def _coerce_authors(raw_authors: list) -> list[Author]:
    authors: list[Author] = []
    for a in raw_authors or []:
        if isinstance(a, dict):
            authors.append(Author(name=a.get("name", "unknown"), email=a.get("email")))
        elif isinstance(a, str):
            authors.append(Author(name=a))
    return authors


def parse_size_mb(value: object) -> int | None:
    """Parse resource strings like '4G', '256M', or plain MB ints."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)

    s = str(value).strip().upper().replace(" ", "")
    if not s:
        return None
    if s.endswith("GB"):
        return int(float(s[:-2]) * 1024)
    if s.endswith("G"):
        return int(float(s[:-1]) * 1024)
    if s.endswith("MB"):
        return int(float(s[:-2]))
    if s.endswith("M"):
        return int(float(s[:-1]))
    if s.endswith("KB"):
        return max(1, int(float(s[:-2]) / 1024))
    if s.endswith("K"):
        return max(1, int(float(s[:-1]) / 1024))
    return int(float(s))


def normalize_raw_task_toml(raw: dict, task_dir: Path | None = None) -> dict:
    """Upgrade legacy Terminal-Bench 1.0 task.toml to the 1.3 parse path."""
    out = dict(raw)
    task = dict(out.get("task") or {})
    metadata = dict(out.get("metadata") or {})
    environment = dict(out.get("environment") or {})

    if "schema_version" not in out and out.get("version"):
        out["schema_version"] = "1.3"
        metadata.setdefault("legacy_version", str(out["version"]))

    if not task.get("authors") and metadata.get("author_name"):
        author: dict = {"name": metadata["author_name"]}
        if metadata.get("author_email"):
            author["email"] = metadata["author_email"]
        task["authors"] = [author]

    if not task.get("keywords") and metadata.get("tags"):
        task["keywords"] = list(metadata["tags"])

    if metadata.get("difficulty") and not metadata.get("difficulty_explanation"):
        metadata["difficulty_explanation"] = str(metadata["difficulty"])

    if not task.get("name"):
        if task_dir is not None:
            task["name"] = task_dir.name
        elif metadata.get("name"):
            task["name"] = str(metadata["name"])

    if not task.get("description") and task_dir is not None:
        instr = task_dir / "instruction.md"
        if instr.exists():
            for line in instr.read_text(errors="replace").splitlines():
                line = line.strip()
                if line:
                    task["description"] = line[:500]
                    break

    if "memory" in environment and "memory_mb" not in environment:
        environment["memory_mb"] = parse_size_mb(environment.pop("memory"))
    if "storage" in environment and "storage_mb" not in environment:
        environment["storage_mb"] = parse_size_mb(environment.pop("storage"))

    out["task"] = task
    out["metadata"] = metadata
    out["environment"] = environment
    return out


def load_raw_toml(content: bytes) -> dict:
    """Parse task.toml into its raw dict (tables preserved) without validation.

    Used by the validator to inspect *where* a key sits — e.g. to catch an
    `artifacts` key nested under the wrong [table], which parse_task_toml would
    silently drop. Raises TaskParseError on malformed TOML.
    """
    try:
        parsed: dict = tomllib.loads(content.decode("utf-8"))
        return parsed
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise TaskParseError(f"task.toml is not valid TOML: {e}") from e


def parse_task_toml(content: bytes, task_dir: Path | None = None) -> TaskConfig:
    """Parse task.toml bytes into a TaskConfig.

    ``task_dir`` is required for legacy Terminal-Bench tasks that omit
    ``[task].name`` — the directory name becomes the task id.

    Raises TaskParseError on malformed TOML or a missing task name.
    """
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise TaskParseError(f"task.toml is not valid TOML: {e}") from e

    raw = normalize_raw_task_toml(raw, task_dir)

    task = raw.get("task", {})
    metadata = raw.get("metadata", {})
    verifier = raw.get("verifier", {})
    agent = raw.get("agent", {})
    environment = raw.get("environment", {})

    name = task.get("name")
    if not name:
        raise TaskParseError(
            "task.toml is missing required field [task].name "
            "(pass task_dir for legacy Terminal-Bench tasks)"
        )

    try:
        return TaskConfig(
            schema_version=str(raw.get("schema_version", "1.3")),
            name=name,
            description=task.get("description"),
            authors=_coerce_authors(task.get("authors", [])),
            keywords=list(task.get("keywords", [])),
            category=metadata.get("category"),
            difficulty_explanation=metadata.get("difficulty_explanation"),
            verifier=VerifierConfig(**verifier),
            agent=AgentConfig(**agent),
            environment=EnvironmentConfig(**environment),
            artifacts=list(raw.get("artifacts", [])),
            metadata=metadata,
        )
    except Exception as e:  # pydantic ValidationError etc.
        raise TaskParseError(f"task.toml has invalid values: {e}") from e
