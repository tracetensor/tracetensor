import sys

sys.path.insert(0, "/app")
from multiply import multiply  # noqa: E402

assert multiply(3, 4) == 12
assert multiply(0, 99) == 0
assert multiply(-2, 5) == -10
assert multiply(7, 7) == 49
print("multiply ok")
