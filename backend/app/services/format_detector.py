"""
Task format detector.

Inspects a task directory and returns which input style it uses:
  "tracetensor" — has task.toml (our native format)
  "hud"         — has env.py (HUD-style format)

Both formats are first-class. The CLI and job runner call this before
deciding which path to take.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

TaskFormat = Literal["tracetensor", "hud"]


class UnknownTaskFormat(ValueError):
    """Raised when neither task.toml nor env.py is found."""


def detect_format(task_dir: Path) -> TaskFormat:
    """Return 'tracetensor' or 'hud' based on which marker file is present.

    Priority: task.toml wins if both exist (native format always takes
    precedence so accidentally-present env.py files don't hijack native tasks).
    """
    if (task_dir / "task.toml").exists():
        return "tracetensor"
    if (task_dir / "env.py").exists():
        return "hud"
    raise UnknownTaskFormat(
        f"No task.toml or env.py found in {task_dir}. "
        "TraceTensor expects either:\n"
        "  • task.toml  — native format\n"
        "  • env.py     — HUD-compatible format"
    )


def is_hud_format(task_dir: Path) -> bool:
    return detect_format(task_dir) == "hud"


def is_tracetensor_format(task_dir: Path) -> bool:
    return detect_format(task_dir) == "tracetensor"
