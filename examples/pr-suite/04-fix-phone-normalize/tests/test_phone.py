import sys

sys.path.insert(0, "/app")
from phone import normalize_phone  # noqa: E402

assert normalize_phone("(415) 555-0100") == "+14155550100"
assert normalize_phone("1-415-555-0100") == "+14155550100"
assert normalize_phone("+44 20 7946 0958") == "+442079460958"

try:
    normalize_phone("")
except ValueError:
    pass
else:
    raise AssertionError("empty should raise")

print("phone normalize ok")
