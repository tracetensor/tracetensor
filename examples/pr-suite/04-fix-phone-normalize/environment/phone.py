"""Broken phone normalizer — drops leading +1 for US local numbers."""


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def normalize_phone(raw: str, default_country: str = "US") -> str:
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty phone")
    if s.startswith("+"):
        d = _digits(s)
        if not d:
            raise ValueError("no digits")
        return "+" + d
    d = _digits(s)
    if not d:
        raise ValueError("no digits")
    if default_country == "US":
        # BUG: returns +digits without country code for 10-digit local
        return "+" + d
    raise ValueError(f"unsupported country {default_country}")
