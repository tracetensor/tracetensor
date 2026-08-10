# Fix normalize_phone — signup validation failing

## PR description

`normalize_phone(raw, default_country="US")` in `/app/phone.py` should return
E.164 format: `+` followed by digits only (no spaces/dashes).

Rules for this task:

- Strip spaces, dashes, parentheses from input.
- If input already starts with `+`, treat as full international number (keep `+`, digits only).
- If no `+`, prepend country code: **US → `+1`**, then the remaining digits.
- US numbers may be 10 digits (area code + number) or 11 digits starting with `1`.
- Empty or no digits → raise `ValueError`.

Fix `/app/phone.py`. Do not change tests.
