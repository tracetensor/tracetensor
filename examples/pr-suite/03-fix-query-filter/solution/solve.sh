#!/bin/bash
cat > /app/query.py << 'EOF'
"""Tiny query filter: field==value with AND / OR (AND binds tighter)."""


def _clause(clause: str, record: dict) -> bool:
    field, _, val = clause.partition("==")
    field, val = field.strip(), val.strip()
    return record.get(field) == val


def _split_or(query: str) -> list[str]:
    parts, buf, toks = [], [], query.split()
    for t in toks:
        if t == "OR":
            parts.append(" ".join(buf))
            buf = []
        else:
            buf.append(t)
    parts.append(" ".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _split_and(chunk: str) -> list[str]:
    parts, buf, toks = [], [], chunk.split()
    for t in toks:
        if t == "AND":
            parts.append(" ".join(buf))
            buf = []
        else:
            buf.append(t)
    parts.append(" ".join(buf))
    return [p.strip() for p in parts if p.strip()]


def matches(query: str, record: dict) -> bool:
    for or_part in _split_or(query):
        if all(_clause(c, record) for c in _split_and(or_part)):
            return True
    return False
EOF
