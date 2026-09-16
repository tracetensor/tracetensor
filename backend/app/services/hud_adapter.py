"""
HUD format adapter.

Loads an env.py (+ optional tasks.py) written in HUD's API style and
extracts everything TraceTensor needs to run it:
  - the TensorEnvironment object (templates, hooks, capabilities)
  - the list of BoundTask objects (concrete runnable tasks)

Usage:
    env_obj, tasks = load_hud_env(Path("my-env"))
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import NamedTuple

from app.services.hud_compat import BoundTask, TensorEnvironment, inject_hud_stubs

log = logging.getLogger("tracetensor.hud_adapter")


class HudEnvLoad(NamedTuple):
    env: TensorEnvironment
    tasks: list[BoundTask]


class HudLoadError(ValueError):
    """Raised when an env.py file cannot be loaded or is not a valid HUD env."""


def _import_module_from_file(path: Path, module_name: str) -> object:
    """Import a Python file as a module, returning the module object."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise HudLoadError(f"Cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise HudLoadError(f"Error executing {path.name}: {exc}") from exc
    return module


def load_hud_env(task_dir: Path) -> HudEnvLoad:
    """Load an env.py (and tasks.py if present) from task_dir.

    Returns (TensorEnvironment, list[BoundTask]).

    Injects TraceTensor's hud stubs into sys.modules before loading,
    so `from hud import Environment` in env.py resolves to our stub.
    """
    env_py = task_dir / "env.py"
    tasks_py = task_dir / "tasks.py"

    if not env_py.exists():
        raise HudLoadError(f"No env.py found in {task_dir}")

    # Inject stubs before any import so env.py sees our Environment.
    inject_hud_stubs()

    # Add task_dir to sys.path so relative imports inside env.py work.
    task_dir_str = str(task_dir.resolve())
    sys_path_added = task_dir_str not in sys.path
    if sys_path_added:
        sys.path.insert(0, task_dir_str)

    # Stash whatever "env" was in sys.modules so we can restore it after.
    _prev_env_module = sys.modules.get("env")

    try:
        env_module = _import_module_from_file(env_py, "_tt_user_env")

        # Pin sys.modules["env"] to THIS env module so tasks.py's
        # `from env import ...` resolves to the correct file, not a stale one
        # from a previous load (sys.modules contamination fix).
        sys.modules["env"] = env_module  # type: ignore[assignment]

        # Find the TensorEnvironment instance in the module
        env_obj = _find_env_object(env_module, env_py)

        # Load tasks.py to get concrete BoundTask instances
        bound_tasks: list[BoundTask] = []
        if tasks_py.exists():
            tasks_module = _import_module_from_file(tasks_py, "_tt_user_tasks")
            bound_tasks = _extract_tasks(tasks_module, env_obj, tasks_py)
        else:
            log.warning("hud_no_tasks_py", extra={"dir": str(task_dir)})

    finally:
        if sys_path_added and task_dir_str in sys.path:
            sys.path.remove(task_dir_str)
        # Clean up our temporary module names so re-loads work
        sys.modules.pop("_tt_user_env", None)
        sys.modules.pop("_tt_user_tasks", None)
        # Restore the previous "env" entry (or remove ours if there was none)
        if _prev_env_module is None:
            sys.modules.pop("env", None)
        else:
            sys.modules["env"] = _prev_env_module

    log.info(
        "hud_env_loaded",
        extra={
            "name": env_obj.name,
            "templates": list(env_obj.templates.keys()),
            "tasks": len(bound_tasks),
            "capabilities": [c.protocol for c in env_obj.capabilities],
        },
    )
    return HudEnvLoad(env=env_obj, tasks=bound_tasks)


def _find_env_object(module: object, env_py: Path) -> TensorEnvironment:
    """Find the TensorEnvironment instance inside a loaded env module."""
    # Convention: the object is named `env`
    env_obj = getattr(module, "env", None)
    if isinstance(env_obj, TensorEnvironment):
        return env_obj

    # Fallback: scan all module attributes
    for attr_name in dir(module):
        val = getattr(module, attr_name, None)
        if isinstance(val, TensorEnvironment):
            log.info("hud_env_found_as", extra={"attr": attr_name})
            return val

    raise HudLoadError(
        f"{env_py.name} must contain a TensorEnvironment (or hud.Environment) "
        f"instance named 'env'. None found."
    )


def _extract_tasks(module: object, env_obj: TensorEnvironment,
                   tasks_py: Path) -> list[BoundTask]:
    """Extract BoundTask instances from a loaded tasks module."""
    tasks: list[BoundTask] = []

    # Convention: tasks.py exposes a `tasks` list
    raw = getattr(module, "tasks", None)
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, BoundTask):
                tasks.append(item)
            else:
                log.warning(
                    "hud_tasks_unknown_item",
                    extra={"type": type(item).__name__, "file": tasks_py.name},
                )
        if tasks:
            return tasks

    # Fallback: collect any BoundTask from any attribute
    for attr_name in dir(module):
        val = getattr(module, attr_name, None)
        if isinstance(val, BoundTask):
            tasks.append(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, BoundTask):
                    tasks.append(item)

    if not tasks:
        log.warning("hud_no_tasks_found", extra={"file": tasks_py.name})

    return tasks
