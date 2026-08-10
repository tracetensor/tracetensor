import sys

sys.path.insert(0, "/app")
from checkout import total  # noqa: E402

items = [{"price": 100, "qty": 1}]
assert total(items) == 110.0
assert total(items, discount_pct=10) == 99.0

bundle = [{"price": 25, "qty": 2}, {"price": 10, "qty": 1}]
assert total(bundle) == 66.0
assert total(bundle, discount_pct=20) == 52.8
print("checkout ok")
