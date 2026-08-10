#!/bin/bash
# Oracle reference fix: replace the broken parser with a correct implementation.
cat > /app/duration.py << 'EOF'
import re

_PART = re.compile(r"(\d+)([smh])", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(s: str) -> int:
    s = (s or "").strip()
    if not s:
        raise ValueError("empty duration")
    parts = list(_PART.finditer(s))
    if not parts:
        raise ValueError(f"invalid duration: {s!r}")
    # Entire string (after strip) must be consumed by valid parts only.
    consumed = "".join(p.group(0) for p in parts)
    if consumed.lower() != s.lower():
        raise ValueError(f"invalid duration: {s!r}")
    total = 0
    for p in parts:
        total += int(p.group(1)) * _UNITS[p.group(2).lower()]
    return total
EOF
