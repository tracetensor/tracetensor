"""Broken log analyzer — only reads first file and misses levels."""

import re

_LEVEL = re.compile(r"^\[(ERROR|WARN|INFO)\]")


def count_levels(paths: list[str]) -> dict[str, int]:
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0}
    if not paths:
        return counts
    # BUG: only first file; also treats unknown tags as INFO
    path = paths[0]
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _LEVEL.match(line.strip())
            if m:
                counts[m.group(1)] += 1
            else:
                counts["INFO"] += 1
    return counts
