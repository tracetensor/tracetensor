import sys

sys.path.insert(0, "/app")
from report import total_value  # noqa: E402

assert total_value() == 125.0
print("inventory report ok")
