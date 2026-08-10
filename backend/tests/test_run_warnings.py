"""Tests for pre-run agent/task warnings."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.services.run_warnings import run_warnings  # noqa: E402
from app.services.task_parser import parse_task_toml  # noqa: E402

PROJECT = BACKEND.parent
passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def _cfg(task_dir: Path):
    return parse_task_toml((task_dir / "task.toml").read_bytes(), task_dir)


print("== run_warnings ==")
sort_csv = PROJECT / "examples" / "sort-csv"
agentic = PROJECT / "examples" / "agentic-suite" / "01-fix-log-analyzer"

check(
    "sort-csv + openai → no multi-file warning",
    # Absence of the MULTI-FILE warning specifically, not of all warnings — an
    # arm64 host legitimately adds a Docker-platform note, which would make a
    # bare `not run_warnings(...)` fail on a dev laptop but pass on amd64 CI.
    not any("multi-file" in w for w in run_warnings(sort_csv, _cfg(sort_csv), "openai")),
)
check(
    "agentic + openai → multi-file warning",
    any("multi-file" in w for w in run_warnings(agentic, _cfg(agentic), "openai")),
)
check(
    "agentic + mini-swe → no multi-file warning",
    not any("multi-file" in w for w in run_warnings(agentic, _cfg(agentic), "mini-swe")),
)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
