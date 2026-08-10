#!/bin/bash
cat > /app/manifest.py << 'EOF'
"""List relative file paths under a project root."""

from pathlib import Path


def list_files(root: str) -> list[str]:
    base = Path(root)
    out: list[str] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        out.append(rel)
    return sorted(out)
EOF
