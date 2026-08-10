import sys

sys.path.insert(0, "/app")
from scheduler import JobQueue  # noqa: E402

q = JobQueue()
q.push("a", 1)
q.push("b", 3)
q.push("c", 2)
assert q.pop() == "b"
assert q.pop() == "c"
assert q.pop() == "a"
assert q.pop() is None

q2 = JobQueue()
q2.push("x", 5)
q2.push("y", 5)
assert q2.pop() == "x"
print("scheduler ok")
