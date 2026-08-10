# Fix retry backoff — CI failing

## PR description

`retry_delay()` in `/app/retry.py` should implement **exponential backoff** for
API retries: delay grows as `base * 2^(attempt-1)` (attempt is 1-based), capped
at `cap` seconds.

The current implementation is wrong (linear growth). Fix `/app/retry.py` only.
Do not change the tests.

### Required behavior

- `retry_delay(1, base=1.0)` → `1.0`
- `retry_delay(2, base=1.0)` → `2.0`
- `retry_delay(3, base=1.0)` → `4.0`
- `retry_delay(4, base=0.5)` → `4.0` (0.5 × 2³)
- `retry_delay(10, base=1.0, cap=30.0)` → `30.0` (capped)
- `attempt` must be ≥ 1; otherwise raise `ValueError`
