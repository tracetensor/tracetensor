import sys

sys.path.insert(0, "/app")
from retry import retry_delay  # noqa: E402


def eq(a, b):
    assert abs(a - b) < 1e-9, f"got {a}, want {b}"


eq(retry_delay(1, base=1.0), 1.0)
eq(retry_delay(2, base=1.0), 2.0)
eq(retry_delay(3, base=1.0), 4.0)
eq(retry_delay(4, base=0.5), 4.0)
eq(retry_delay(10, base=1.0, cap=30.0), 30.0)

try:
    retry_delay(0)
except ValueError:
    pass
else:
    raise AssertionError("attempt 0 should raise ValueError")

print("backoff ok")
