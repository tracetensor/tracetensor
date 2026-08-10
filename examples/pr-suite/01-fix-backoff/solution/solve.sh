#!/bin/bash
cat > /app/retry.py << 'EOF'
"""Exponential retry backoff with cap."""


def retry_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    delay = base * (2 ** (attempt - 1))
    return min(delay, cap)
EOF
