# Fix session auth helper — login API regression

## PR description

`/app/auth.py` implements `resolve_user(session_id)`:

1. Load `/data/sessions.json` — map session_id → user_id
2. Load `/data/users.json` — map user_id → user record dict
3. Return the user dict if session exists **and** user exists; else `None`

Fix `/app/auth.py`. Do not change `/data/*` or tests.

Test session `"sess-7"` should resolve to `{"id": "u2", "role": "admin"}`.
