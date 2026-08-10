import sys

sys.path.insert(0, "/app")
from query import matches  # noqa: E402

# AND binds tighter than OR: a==1 OR (b==2 AND c==3)
q = "a==1 OR b==2 AND c==3"
rec = {"a": "1", "b": "9", "c": "9"}
assert matches(q, rec) is True, "a==1 branch should match"

rec2 = {"a": "9", "b": "2", "c": "3"}
assert matches(q, rec2) is True, "b AND c branch should match"

rec3 = {"a": "9", "b": "2", "c": "9"}
assert matches(q, rec3) is False

q2 = "status==open AND owner==alice OR owner==bob"
assert matches(q2, {"status": "open", "owner": "bob"}) is True

print("query filter ok")
