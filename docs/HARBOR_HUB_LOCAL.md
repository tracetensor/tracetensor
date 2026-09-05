# Harbor Hub — local pull and run

TraceTensor can download public tasks and datasets from [Harbor Hub](https://hub.harborframework.com/) without the Harbor CLI.

## Pull a task

```bash
tracetensor tasks pull orca-bench/032b3bef243e177f
tracetensor tasks validate tasks/032b3bef243e177f
tracetensor run tasks/032b3bef243e177f -a oracle --platform linux/amd64
```

Package format: `org/name` or `org/name@latest` (optional `hub:` prefix).

## Pull a dataset

```bash
tracetensor dataset pull terminal-bench/terminal-bench-2-1@latest -o tasks/
tracetensor dataset run tasks/terminal-bench-2-1 -a oracle
```

## Apple Silicon (arm64 Mac)

Many Hub images are **linux/amd64 only**. TraceTensor auto-defaults to `linux/amd64` on arm64 hosts.

- Override: `tracetensor run … --platform linux/amd64`
- Or in `task.toml`: `[environment] platform = "linux/amd64"`
- Env: `TRACETENSOR_DOCKER_PLATFORM=native` to disable auto emulation

Expect **slower** runs and ensure Docker Desktop has enough RAM (8GB+ for heavy snapshot tasks).

## Local run tiers

| Tier | Meaning |
|------|---------|
| **native** | Should run normally on your machine |
| **heavy** | amd64 emulation, large image, or high RAM — may be slow |
| **unsupported** | Compose / features TraceTensor does not run locally yet |

`tracetensor tasks validate` and `tracetensor run` print tier warnings before execution.

## Cache

Downloads cache under `~/.cache/tracetensor/registry/` (override with `TRACETENSOR_REGISTRY_CACHE`).

Use `--cache` on `tasks pull` to store in cache layout instead of export layout.

## Environment

See [`.env.example`](../.env.example):

- `TRACETENSOR_REGISTRY_URL` — Supabase registry base URL
- `TRACETENSOR_REGISTRY_TOKEN` — for private packages (future)
- `TRACETENSOR_DOCKER_PLATFORM` — Docker platform override

## Stress tests (T2)

```bash
export TRACETENSOR_HUB_STRESS=1
chmod +x scripts/hub_stress.sh
./scripts/hub_stress.sh
```

See [`backend/tests/README.md`](../backend/tests/README.md) for CI tiers.
