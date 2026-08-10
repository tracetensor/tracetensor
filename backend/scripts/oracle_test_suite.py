#!/usr/bin/env python3
"""Oracle-smoke every task in examples/test-suite — CI gate (no API key)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "examples" / "test-suite"
BACKEND = ROOT / "backend"


def main() -> int:
    if not SUITE.is_dir():
        print(f"test-suite not found: {SUITE}", file=sys.stderr)
        return 1
    tasks = sorted(d for d in SUITE.iterdir() if d.is_dir() and (d / "task.toml").exists())
    if not tasks:
        print("no tasks in test-suite", file=sys.stderr)
        return 1
    print(f"oracle smoke: {len(tasks)} tasks under {SUITE.name}")
    cmd = [
        sys.executable,
        "-m",
        "app.cli.main",
        "dataset",
        "run",
        str(SUITE),
        "-a",
        "oracle",
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True)
    if proc.stdout.strip():
        print(proc.stdout.strip()[-2000:])
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    if proc.returncode != 0:
        print(f"oracle test-suite FAILED (exit {proc.returncode})", file=sys.stderr)
        return proc.returncode
    print("oracle test-suite OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
