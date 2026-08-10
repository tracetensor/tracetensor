"""Grade parse_duration() — must match PR acceptance tests."""
import sys

sys.path.insert(0, "/app")
from duration import parse_duration  # noqa: E402


def expect(expr: str, seconds: int) -> None:
    got = parse_duration(expr)
    assert got == seconds, f"parse_duration({expr!r}) -> {got}, want {seconds}"


def expect_error(expr: str) -> None:
    try:
        parse_duration(expr)
    except ValueError:
        return
    raise AssertionError(f"parse_duration({expr!r}) should raise ValueError")


expect("30s", 30)
expect("2m", 120)
expect("1h", 3600)
expect("1h30m", 5400)
expect("2m10s", 130)
expect(" 1h2m3s ", 3723)
expect("10s2m", 130)  # order should not matter

expect_error("")
expect_error("abc")
expect_error("1d")  # unknown unit
expect_error("1.5h")  # non-integer not supported

print("all parse_duration() checks passed")
