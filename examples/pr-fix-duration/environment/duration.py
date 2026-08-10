"""Broken duration parser — looks plausible but is wrong (CI red)."""


def parse_duration(s: str) -> int:
    """Return total seconds for strings like '1h30m'.

    BUG: only reads the first integer and ignores units entirely, so
    '1h' -> 1 and '2m10s' -> 2 instead of 3600 / 130.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("empty duration")
    digits = []
    for ch in s:
        if ch.isdigit():
            digits.append(ch)
        elif digits:
            break
    if not digits:
        raise ValueError(f"invalid duration: {s!r}")
    return int("".join(digits))
