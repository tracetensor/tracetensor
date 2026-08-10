#!/bin/bash
cat > /app/stats.py << 'EOF'
"""Linear-interpolation percentile."""


def percentile(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("empty values")
    if p < 0 or p > 100:
        raise ValueError("p out of range")
    s = sorted(values)
    n = len(s)
    r = (p / 100.0) * (n - 1)
    lo = int(r)
    if lo >= n:
        return float(s[-1])
    hi = min(lo + 1, n - 1)
    frac = r - lo
    return s[lo] * (1 - frac) + s[hi] * frac
EOF
