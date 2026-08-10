#!/bin/bash
cat > /app/phone.py << 'EOF'
"""Normalize phone numbers to E.164 (US default)."""


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def normalize_phone(raw: str, default_country: str = "US") -> str:
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty phone")
    if s.startswith("+"):
        d = _digits(s)
        if not d:
            raise ValueError("no digits")
        return "+" + d
    d = _digits(s)
    if not d:
        raise ValueError("no digits")
    if default_country == "US":
        if len(d) == 11 and d.startswith("1"):
            return "+" + d
        if len(d) == 10:
            return "+1" + d
        raise ValueError("invalid US phone length")
    raise ValueError(f"unsupported country {default_country}")
EOF
