"""Pre-run warnings: agent ↔ task compatibility (CLI + batch runners)."""

from __future__ import annotations

from pathlib import Path

from app.schemas.task import TaskConfig
from app.services.agents.registry import lookup
from app.services.docker_platform import host_is_arm64, resolve_docker_platform


def _is_installed_agent(name: str) -> bool:
    spec = lookup(name)
    return spec is not None and spec.installed


def _is_bash_loop_agent(name: str) -> bool:
    if _is_installed_agent(name):
        return False
    if lookup(name) is not None:
        return False
    if ":" in name:
        return False
    from app.services import llm

    return llm.canonical_provider(name) in llm.PROVIDERS


def _network_is_no_network(cfg: TaskConfig) -> bool:
    base = (cfg.environment.network_mode or "public").lower()
    agent = (cfg.agent.network_mode or base).lower()
    return base == "no-network" or agent == "no-network"


def _looks_like_multi_file_fix(task_dir: Path, cfg: TaskConfig) -> bool:
    rel = str(task_dir).lower()
    if "agentic-suite" in rel or "/fix-" in rel or rel.endswith("-fix"):
        return True
    cat = (cfg.category or "").lower()
    if cat in ("agentic", "multi-file", "codebase"):
        return True
    instruction = task_dir / "instruction.md"
    if instruction.exists():
        text = instruction.read_text(errors="replace").lower()
        markers = (
            "multiple files",
            "several files",
            "across the codebase",
            "refactor",
            "fix the bug",
            "fix all",
        )
        if sum(1 for m in markers if m in text) >= 2:
            return True
    return False


def run_warnings(task_dir: Path, cfg: TaskConfig, agent: str) -> list[str]:
    """Actionable warnings before starting a trial (keys already validated)."""
    out: list[str] = []

    if _is_installed_agent(agent) and _network_is_no_network(cfg):
        out.append(
            f"Agent '{agent}' runs inside the container and needs network access — "
            "this task uses network_mode=no-network. Use anthropic/openai/openrouter "
            '(bash loop, LLM calls on the host) or set agent.network_mode = "public" in task.toml.'
        )

    if _looks_like_multi_file_fix(task_dir, cfg) and _is_bash_loop_agent(agent):
        out.append(
            "This looks like a multi-file fix task — the built-in bash loop often "
            "under-performs. Prefer mini-swe, claude-code, or codex for agentic fixes."
        )

    if host_is_arm64() and not cfg.environment.platform:
        plat = resolve_docker_platform(None)
        if plat:
            out.append(
                f"Apple Silicon host — no [environment].platform in task.toml; "
                f"using Docker platform {plat!r} for amd64-only images. "
                'Set platform = "native" via TRACETENSOR_DOCKER_PLATFORM=native to disable.'
            )

    return out
