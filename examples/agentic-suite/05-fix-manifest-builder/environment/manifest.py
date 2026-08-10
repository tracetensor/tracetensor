"""Broken manifest — returns absolute paths and includes directories."""

from pathlib import Path


def list_files(root: str) -> list[str]:
    base = Path(root)
    out: list[str] = []
    for p in base.rglob("*"):
        # BUG: includes dirs and absolute paths
        out.append(str(p))
    return sorted(out)
