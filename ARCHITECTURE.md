# Architecture

TraceTensor evaluates coding agents. It takes a task (an agent's job + how to
grade it), runs an agent through it in an isolated Docker sandbox, and scores the
result — for one task or a versioned dataset, with an aggregate leaderboard.
Saved runs live in a local **Vault** (CLI `runs/` and/or server jobs).

Phaseing (local Vault vs remote share / cloud sandboxes) is documented in
[docs/OSS_ROADMAP.md](docs/OSS_ROADMAP.md).

## The pipeline

```
  Register ─────────►  Run ──────────────►  Score ──────────►  Compare / Vault
  (validate a task)    (agent in a          (verifier grades   (leaderboard +
                        container, N×)        the output)        export)
```

Task format is Harbor-compatible: `instruction.md`, `task.toml`, an environment
(`environment/Dockerfile` or a `docker_image`), a verifier (`tests/test.sh`), and
an optional reference `solution/solve.sh`.

## Request → work, end to end

**Register** — `POST /ingest/task/upload` → `task_store` extracts the zip safely
→ `task_parser` reads `task.toml` → `task_validator` checks required files → a
`Task` row is written with status `ready_for_examination`.

**Run** — `POST /examine/{task_id}` creates a `Job`, then a background worker runs
N trials with bounded concurrency. Each **trial** (`trial_runner.run_trial`):

1. `environment` builds the task image and starts a container
   (`--network none` by default, non-root agent user, cpu/memory limits).
2. `agent` runs — oracle, an LLM bash loop, or an installed coding agent
   (mini-swe, claude-code, codex, …).
3. `verifier` runs `test.sh` (in the same container, or a fresh **isolated**
   grader container so the agent can't tamper with its own test) and parses a
   reward from `/logs/verifier/reward.{txt,json}` (optional TT `tests/reward.toml`
   for named criteria). CTRF is not read today.
4. The trajectory (steps + optional `llm_usage_summary`), reward, and any
   warnings are recorded to the `Trial` row.

Progress streams live to the UI over Server-Sent Events (`event_bus` → `/examine/job/{id}/stream`).

**Dataset run** — `POST /datasets/{id}/examine` fans every `(task, trial)` pair
through one global concurrency cap, one `Job` per task, all under a `DatasetRun`.
`GET /datasets/{id}/leaderboard` aggregates the latest run per (agent, model)
into an agent × model × task grid.

**Vault** — product layer over saved runs (not a second database):

- Local CLI: `tracetensor run` writes `runs/*/result.json`; `tracetensor vault
  list|show|export` reads that tree.
- Server: `GET /v1/vault/jobs` (enriched job summaries) and
  `GET /v1/vault/export/{job_id}`; dashboard **Vault** nav lists Postgres jobs.
- Usage rollups (tokens / estimate `$`) come from `trajectory.llm_usage_summary`
  via `services/vault.py`. Remote share is Phase 2.

## Layers

Strict direction: **routers → services → models**. Business logic lives in
services; routers only validate input (Pydantic) and shape responses; models are
persistence only.

```
app/
├── main.py            FastAPI app, CORS, lifespan (init_db), serves the frontend
├── core/
│   ├── config.py      Settings from env / backend/.env
│   └── database.py    async engine, session, Base, init_db (+ additive-column shim)
├── routers/           HTTP endpoints — thin
│   ├── ingest.py        register / list / inspect tasks; example task
│   ├── examine.py       start jobs, job views, SSE stream, providers catalog
│   ├── datasets.py      bundles, dataset runs, leaderboard, example dataset
│   └── vault.py         Vault list + portable job export
├── services/          business logic — where the work happens
│   ├── trial_runner.py  orchestrates one trial (setup → agent → verify → score)
│   ├── environment.py   Docker sandbox: build/run/exec/network/limits/teardown
│   ├── agent.py         oracle, LLM bash-loop, installed agents + status catalog
│   ├── llm.py           provider adapters (anthropic / openai / openrouter)
│   ├── verifier.py      runs test.sh, parses reward (+ optional reward.toml)
│   ├── vault.py         usage rollup; local runs/ list/show/export
│   ├── task_parser.py   task.toml → TaskConfig
│   ├── task_validator.py  required-file checks (+ unsupported-feature warnings)
│   ├── dataset_parser.py  bundle manifest + content hash
│   └── event_bus.py     in-memory per-job SSE event log
├── cli/               tracetensor run | serve | tasks | dataset | vault
├── models/            SQLAlchemy ORM: Task, Job, Trial, Dataset, DatasetRun
├── schemas/           Pydantic request/response shapes
└── storage/           on-disk task store (safe zip extract)
```

## Data model

- **Task** — a registered task: parsed config (JSONB), on-disk dir, file-presence
  flags, lifecycle status.
- **Job** — one examination order for a task (agent, model, n_trials). Optional
  `dataset_run_id` links it to a dataset run.
- **Trial** — one attempt: status, reward, `passed`, full `trajectory` (JSONB),
  verifier log, reward payload, `duration_s`.
- **Dataset** — a named, versioned bundle of tasks; `content_hash` makes a
  `(name, version)` immutable.
- **DatasetRun** — one agent+model run across a dataset's tasks.

JSONB for flexible blobs (config, trajectory, reward payload); real columns for
anything filtered or sorted (name, status, ids). Postgres in production; SQLite
works for local dev (the JSON column degrades to `JSON` via a dialect variant).

## The sandbox (why it's a container, not a subprocess)

Agent code is untrusted, so every trial runs in a real Docker container, never a
host subprocess. The environment layer enforces Harbor-compatible isolation:
`--network none` vs `public`, cpu/memory/storage limits, a non-root agent user, a
root-owned reward directory the agent can't forge, and an optional **isolated
verifier** — a fresh grader container built from `tests/Dockerfile` that receives
only the agent's declared artifacts, so tampering with the agent's own `test.sh`
can't affect the grade.

## Known limitations (honest)

- **Local CLI Vault ≠ dashboard Vault** unless you use `--server` (or the UI).
- **Export is TraceTensor JSON**, not Harbor ATIF / Viewer (Phase 2).
- **MCP servers / Computer-1 / cloud envs** are unsupported; validator warns when
  it sees Harbor MCP keys in `task.toml`.
- Podman is wired in code but not claimed as verified on every host.
- See [docs/HARBOR_PARITY_PLAN.md](docs/HARBOR_PARITY_PLAN.md) and
  [docs/OSS_ROADMAP.md](docs/OSS_ROADMAP.md).
