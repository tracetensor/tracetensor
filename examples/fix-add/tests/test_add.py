import sys
sys.path.insert(0, "/app")
from add import add  # noqa: E402

assert add(2, 3) == 5, f"add(2,3) returned {add(2, 3)}"
assert add(10, 5) == 15, f"add(10,5) returned {add(10, 5)}"
assert add(-1, 1) == 0, f"add(-1,1) returned {add(-1, 1)}"
print("all add() checks passed")
