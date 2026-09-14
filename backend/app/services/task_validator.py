"""
Evaluation validator.

An evaluation is ready to run when it has:
  - instruction.md          REQUIRED, non-empty
  - task.toml               REQUIRED, parseable
  - environment             Dockerfile OR docker_image in task.toml
  - tests/test.sh|test.bat  REQUIRED
  - solution/solve.sh       OPTIONAL (warn if missing)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.models.enums import TaskStatus
from app.services.task_parser import TaskParseError, load_raw_toml, parse_task_toml

# Kept as module-level names because callers and tests import them directly; the
# values now come from the one enum that defines the lifecycle (models/enums.py).
STATUS_REGISTERED = TaskStatus.REGISTERED.value
STATUS_READY = TaskStatus.READY.value


@dataclass
class ValidationResult:
    is_valid: bool
    status: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_instruction: bool = False
    has_task_toml: bool = False
    has_dockerfile: bool = False
    has_docker_image: bool = False
    has_test_script: bool = False
    has_solution: bool = False
    local_tier: str | None = None
    # Advisories about THIS machine (arm64 emulation, Docker memory, heavy image),
    # kept separate from `warnings`. A warning is a property of the task and must
    # read the same everywhere; a host note is a property of where you happen to
    # be standing. Merging them meant the identical task validated differently on
    # an arm64 laptop and an x86 CI box — and got a different stored warning set
    # depending on which machine registered it.
    host_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "status": self.status,
            "errors": self.errors,
            "warnings": self.warnings,
            "host_notes": self.host_notes,
            "health_checks": {
                "has_instruction": self.has_instruction,
                "has_task_toml": self.has_task_toml,
                "has_dockerfile": self.has_dockerfile,
                "has_docker_image": self.has_docker_image,
                "has_test_script": self.has_test_script,
                "has_solution": self.has_solution,
            },
            "local_tier": self.local_tier,
        }


def validate_task(task_dir: Path) -> ValidationResult:
    """Inspect an evaluation directory on disk and report whether it can proceed."""
    result = ValidationResult(is_valid=False, status=STATUS_REGISTERED)

    instruction = task_dir / "instruction.md"
    if not instruction.exists():
        result.errors.append("Missing instruction.md — describe what the agent should do.")
    elif instruction.stat().st_size == 0:
        result.errors.append("instruction.md is empty.")
    else:
        result.has_instruction = True

    toml_file = task_dir / "task.toml"
    parsed_config = None
    if not toml_file.exists():
        result.errors.append("Missing task.toml — name, timeouts, and limits live here.")
    else:
        try:
            parsed_config = parse_task_toml(toml_file.read_bytes(), task_dir)
            result.has_task_toml = True
        except TaskParseError as e:
            result.errors.append(str(e))

    dockerfile = task_dir / "environment" / "Dockerfile"
    if dockerfile.exists():
        result.has_dockerfile = True
    if parsed_config and parsed_config.environment.docker_image:
        result.has_docker_image = True
    if not result.has_dockerfile and not result.has_docker_image:
        result.errors.append(
            "No environment defined — provide environment/Dockerfile "
            "or set [environment].docker_image in task.toml."
        )

    test_sh = task_dir / "tests" / "test.sh"
    test_bat = task_dir / "tests" / "test.bat"
    _is_llm_judge = parsed_config and parsed_config.verifier.type == "llm-judge"
    if test_sh.exists() or test_bat.exists():
        result.has_test_script = True
    elif _is_llm_judge:
        result.has_test_script = True  # llm-judge tasks score via the model, no test.sh needed
    else:
        result.errors.append("Missing tests/test.sh — there is no way to score the run.")

    solve = task_dir / "solution" / "solve.sh"
    if solve.exists():
        result.has_solution = True
    else:
        result.warnings.append("No solution/solve.sh — optional reference solution recommended.")

    # Isolated-grader artifact sanity (silent-0.0 footgun).
    if parsed_config and parsed_config.verifier.environment_mode == "separate":
        raw = load_raw_toml(toml_file.read_bytes()) if toml_file.exists() else {}
        artifacts = parsed_config.artifacts
        if "artifacts" not in raw:
            result.warnings.append(
                "verifier.environment_mode is 'separate' but no root-level "
                "`artifacts` list is set — the grader may score 0.0 silently."
            )
        elif not artifacts:
            result.warnings.append(
                "verifier.environment_mode is 'separate' but `artifacts` is empty."
            )

    # M-4: validate environment variable key names at registration time so a bad
    # task.toml fails immediately rather than crashing mid-trial inside Docker.
    if parsed_config:
        import re

        _valid_key = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for k in (parsed_config.environment.env or {}).keys():
            if not _valid_key.match(k):
                result.errors.append(
                    f"Invalid environment variable name in [environment].env: {k!r} — "
                    "must match [A-Za-z_][A-Za-z0-9_]*"
                )

    # Reject unsupported network modes at registration time rather than surfacing a
    # RuntimeError mid-trial when the container tries to start.
    if parsed_config and parsed_config.environment.network_mode == "allowlist":
        result.errors.append(
            "network_mode 'allowlist' is not yet supported — use 'public' or 'no-network'."
        )

    # Features we do not support yet — warn so authors don't get silent 0.0.
    if toml_file.exists():
        try:
            raw = load_raw_toml(toml_file.read_bytes())
        except TaskParseError:
            raw = {}
        if _raw_has_mcp(raw):
            result.warnings.append(
                "task.toml references MCP servers — TraceTensor does not run MCP "
                "yet; scores may differ for this task."
            )
        if _raw_has_computer(raw):
            result.warnings.append(
                "task.toml looks like a Computer-1 / GUI task — unsupported here."
            )
        if _raw_has_steps(raw):
            result.warnings.append(
                "task.toml defines [steps] — multi-step trials are not supported; "
                "only the first instruction/environment is run."
            )
        if _task_has_compose(task_dir):
            result.warnings.append(
                "environment/docker-compose.yaml found — TraceTensor uses a single "
                "container (no Compose sidecars yet)."
            )
        if _raw_is_windows(raw, task_dir):
            result.warnings.append(
                "Windows container task detected — TraceTensor CLI supports Linux "
                "Docker only on this path."
            )

    # CTRF (Common Test Report Format) is not scored — only reward.txt/json (+ reward.toml).
    for ctrf in (
        task_dir / "tests" / "ctrf.json",
        task_dir / "tests" / "report.ctrf.json",
    ):
        if ctrf.exists():
            result.warnings.append(
                f"Found {ctrf.relative_to(task_dir)} — CTRF is not read; "
                "write /logs/verifier/reward.txt or reward.json from test.sh."
            )
            break

    result.is_valid = len(result.errors) == 0
    if result.is_valid:
        result.status = STATUS_READY
    if parsed_config:
        from app.services.local_preflight import assess_local_run

        tier, preflight = assess_local_run(task_dir, parsed_config)
        result.local_tier = tier.value
        for msg in preflight:
            if msg not in result.host_notes:
                result.host_notes.append(msg)
    return result


def _raw_has_mcp(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    if raw.get("mcp_servers") or raw.get("mcp"):
        return True
    for section in ("agent", "environment", "verifier", "task"):
        block = raw.get(section)
        if isinstance(block, dict) and (
            block.get("mcp_servers") or block.get("mcp") or block.get("mcp_server")
        ):
            return True
    return False


def _raw_has_steps(raw: dict) -> bool:
    steps = raw.get("steps") if isinstance(raw, dict) else None
    return isinstance(steps, list) and len(steps) > 0


def _task_has_compose(task_dir: Path) -> bool:
    env = task_dir / "environment"
    return (env / "docker-compose.yaml").exists() or (env / "docker-compose.yml").exists()


def _raw_is_windows(raw: dict, task_dir: Path) -> bool:
    env = raw.get("environment") if isinstance(raw, dict) else None
    if not isinstance(env, dict):
        env = {}
    if str(env.get("os") or "").lower() == "windows":
        return True
    has_bat = (task_dir / "tests" / "test.bat").exists()
    has_sh = (task_dir / "tests" / "test.sh").exists()
    return has_bat and not has_sh


def _raw_has_computer(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    # Assign first, then narrow: `x.get(k) if isinstance(x.get(k), dict) else {}`
    # calls get() twice and mypy can't carry the narrowing across the two calls.
    env = raw.get("environment")
    if not isinstance(env, dict):
        env = {}
    typ = str(env.get("type") or env.get("backend") or "").lower()
    if typ in ("computer", "computer-1", "gui"):
        return True
    meta = raw.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    cat = str(meta.get("category") or "").lower()
    return "computer-1" in cat or cat == "computer"
