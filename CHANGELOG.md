# Changelog

## 0.1.0 — 2026-08-10

Initial open-source release. Local Docker evaluation of coding agents, end to end.

### Tasks

- Harbor-compatible task format: `instruction.md`, `task.toml`, `environment/`,
  `tests/`, optional `solution/`
- `tracetensor tasks init` scaffolds a task; `tracetensor tasks validate` checks it
  is runnable and warns on unsupported Harbor keys
- 26 example tasks across `examples/`, plus 89 imported Terminal-Bench tasks in `tasks/`

### Execution

- One Docker container per trial — never a host subprocess
- `--network none` by default, non-root agent user, cpu/memory/storage limits,
  root-owned reward directory the agent cannot forge
- Podman registered as a second environment backend
- Trial pipeline: setup → agent → verify → score → teardown, with full trajectory capture
- Multiple trials per job under a bounded concurrency cap
- Live progress over Server-Sent Events

### Agents

- Verified on a real in-container run: `oracle`, `mini-swe`, `claude-code`, `codex`
- Provider bash loops via litellm: `anthropic`, `openai`, `openrouter`
- Agent registry carries an honest per-agent status; unverified adapters are not registered

### Scoring

- `tests/test.sh` writing `reward.json` / `reward.txt`
- Reward Kit (`tests/reward.toml`) for weighted named criteria
- LLM-judge verifier for rubric grading
- Optional isolated verifier container so an agent cannot tamper with its own grade

### Datasets

- `tracetensor dataset run` across a task directory, with an agent × model × task leaderboard
- `tracetensor dataset pull` from a local path, a git repo, or a slice of SWE-Bench Verified

### Vault

- CLI runs write `runs/*/result.json`
- `tracetensor vault list | show | export`
- Token, duration and estimated-cost rollups from the trajectory
- Export format is TraceTensor JSON

### Operations

- Postgres-backed job queue with dedicated workers (`WORKER_EMBEDDED=false`)
- Dead-worker reclaim, infrastructure-vs-task failure retry, clean worker drain
- Optional `API_TOKEN` on all `/v1` routes; optional per-minute job-creation rate limit
- Guardrail scanner flags agent commands touching SSH keys, the Docker socket, `rm -rf /`

### CLI

`run`, `serve`, `version`, `tasks init`, `tasks validate`, `dataset run`,
`dataset pull`, `vault list`, `vault show`, `vault export`

## Known limitations at 0.1.0

- **Harbor-compatible, not Harbor-equivalent.** The differential golden suite that
  would justify an equivalence claim has not been built.
- **The web dashboard is not in this repository.** `tracetensor serve` runs the API;
  the UI is served only when a `frontend/` directory is present.
- **Local containers only.** No cloud sandboxes, GPU/TPU requests, Docker Compose
  environments, MCP servers, Windows containers, or `allowlist` networking.
- **Multi-step tasks** and multi-step reward strategies are not supported.
- **Exports are TraceTensor JSON.** ATIF and Harbor Viewer parity are not claimed.
- **No CI workflow is committed.** The suite runs locally via
  `python tests/run_ci.py` (coverage floor 70%).
- Guardrails are tripwires, not a WAF. Prompt injection, malicious third-party task
  bundles and container escape are out of scope — see [SECURITY.md](SECURITY.md).
