#!/bin/bash
cat > /app/merge.py << 'EOF'
"""Deep merge two dicts (recursive for nested dicts)."""


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, val in patch.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out
EOF
