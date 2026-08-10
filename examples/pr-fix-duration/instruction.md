# Fix parse_duration — CI failing

## PR description

`parse_duration()` in `/app/duration.py` should convert human duration strings
into total seconds. CI is red because the current implementation is wrong.

### Required behavior

- Accept a string with one or more parts using units `s` (seconds), `m` (minutes),
  `h` (hours), e.g. `30s`, `2m`, `1h`, `1h30m`, `2m10s`.
- Return an **int** total number of seconds.
- Parts may appear in any order; values are non-negative integers.
- Whitespace around the string is allowed; no spaces required between parts.
- If the string is empty, has no valid parts, or contains invalid characters /
  unknown units, raise `ValueError`.

### Examples

| Input | Expected |
| --- | --- |
| `30s` | `30` |
| `2m` | `120` |
| `1h` | `3600` |
| `1h30m` | `5400` |
| `2m10s` | `130` |
| `abc` | `ValueError` |

Fix `/app/duration.py` so the tests pass. Do not change the tests.
