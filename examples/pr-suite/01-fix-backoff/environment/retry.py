"""Broken retry backoff — linear instead of exponential."""


def retry_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    # BUG: linear backoff (base * attempt) instead of exponential
    return min(base * attempt, cap)
