#!/bin/bash
cat > /app/analyzer.py << 'EOF'
"""Count log levels across multiple log files."""

import re

_LEVEL = re.compile(r"^\[(ERROR|WARN|INFO)\]")


def count_levels(paths: list[str]) -> dict[str, int]:
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = _LEVEL.match(line.strip())
                if m:
                    counts[m.group(1)] += 1
    return counts
EOF
