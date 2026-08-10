"""Broken percentile — uses p/100 * n instead of (n-1) for index."""


def percentile(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("empty values")
    if p < 0 or p > 100:
        raise ValueError("p out of range")
    s = sorted(values)
    n = len(s)
    # BUG: wrong rank formula (uses n instead of n-1)
    r = (p / 100.0) * n
    if r >= n:
        return float(s[-1])
    lo = int(r)  # floor via truncation toward zero for positive r
    hi = min(lo + 1, n - 1)
    frac = r - lo
    return s[lo] * (1 - frac) + s[hi] * frac
