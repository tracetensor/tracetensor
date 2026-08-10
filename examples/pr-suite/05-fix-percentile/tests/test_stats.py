import sys

sys.path.insert(0, "/app")
from stats import percentile  # noqa: E402

data = [10.0, 20.0, 30.0, 40.0]


def eq(a, b):
    assert abs(a - b) < 1e-9, f"got {a}, want {b}"


eq(percentile(data, 0), 10.0)
eq(percentile(data, 50), 25.0)
eq(percentile(data, 100), 40.0)
eq(percentile([5.0, 5.0, 5.0], 50), 5.0)

try:
    percentile([], 50)
except ValueError:
    pass
else:
    raise AssertionError("empty should raise")

print("percentile ok")
