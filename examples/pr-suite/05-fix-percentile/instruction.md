# Fix percentile — metrics dashboard wrong

## PR description

`percentile(values, p)` in `/app/stats.py` returns the **p-th percentile**
(0 ≤ p ≤ 100) using **linear interpolation** between closest ranks
(NIST/nearest-rank style simplified for this task):

1. Sort `values` ascending.
2. If empty → raise `ValueError`.
3. Compute rank index `r = (p / 100) * (n - 1)` where `n = len(values)`.
4. Let `lo = floor(r)`, `hi = ceil(r)`, `frac = r - lo`.
5. Return `sorted[lo] * (1 - frac) + sorted[hi] * frac` (if lo==hi, just that value).

Fix `/app/stats.py`. Do not change tests.

Examples on `[10, 20, 30, 40]`:
- p=0 → 10
- p=50 → 25
- p=100 → 40
