"""The three tools the research agent works with, as LangChain tools."""

from __future__ import annotations

import pathlib

from langchain_core.tools import tool

MAX_READ_CHARS = 20_000


def _safe(path: str) -> pathlib.Path:
    """Resolve a tool path against the cwd the runner set (the task workdir)."""
    return pathlib.Path(path).expanduser()


@tool
def list_files(directory: str) -> str:
    """List the files in a directory. Use this first to discover the sources."""
    p = _safe(directory)
    if not p.is_dir():
        return f"ERROR: {directory} is not a directory."
    names = sorted(f.name for f in p.iterdir() if f.is_file())
    return "\n".join(names) if names else f"(no files in {directory})"


@tool
def read_file(path: str) -> str:
    """Read a UTF-8 text file and return its contents."""
    p = _safe(path)
    if not p.is_file():
        return f"ERROR: {path} does not exist."
    return p.read_text(encoding="utf-8")[:MAX_READ_CHARS]


@tool
def write_file(path: str, content: str) -> str:
    """Write text to a file, creating parent directories as needed."""
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}."
