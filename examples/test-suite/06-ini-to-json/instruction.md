An INI file is at `/data/app.ini`.

Parse it and write `/data/config.json` with this shape:

- Top-level keys = INI section names
- Each section is an object of key/value pairs
- Convert these keys to **JSON integers** when their values are numeric:
  `port`, `retries`, `ttl`
- Convert `true` / `false` (case-insensitive) to JSON booleans

Example section `[database]` with `host = localhost` becomes
`"database": {"host": "localhost", ...}`.

Only include sections and keys present in the INI file.
