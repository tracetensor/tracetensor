"""Broken deep merge — replaces nested dicts instead of merging."""


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, val in patch.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            # BUG: should recurse; instead we fall through and replace
            pass
        out[key] = val
    return out
