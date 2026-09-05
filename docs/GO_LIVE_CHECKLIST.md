# Go-live checklist — open source launch

Master checklist for publishing TraceTensor on GitHub. Target: **Friday before 8 AM**.

Use this doc end-to-end before the first public push. Check items off as you go.

Related: [LAUNCH_PLAN.md](LAUNCH_PLAN.md) · [SECRETS_AUDIT.md](SECRETS_AUDIT.md) · [SECURITY.md](../SECURITY.md) · [OSS_ROADMAP.md](OSS_ROADMAP.md)

---

## Verdict

| Area | Status |
|---|---|
| **Core product (Harbor-style local eval)** | ✅ Ready |
| **CI (13/13 suites, Docker + offline)** | ✅ Green locally |
| **Security docs + threat model** | ✅ Written |
| **Repo hygiene (first commit)** | ⚠️ Not done yet — no commits exist |
| **Secrets in working tree** | ⚠️ Real keys in `backend/.env` — must stay untracked |

**Biggest risks:** accidental secret commit, publishing internal folders, users deploying API without auth on the internet.

---

## P0 — Blockers (must complete before push)

### Secrets & credentials

- [ ] Confirm `backend/.env` is **never staged** (`git check-ignore -v backend/.env`)
- [ ] Scan repo for keys (exclude local `.env`):
  ```bash
  rg -n 'sk-(ant|proj)-|ghp_[a-zA-Z0-9]{20,}' --glob '!.git' --glob '!backend/.env' .
  ```
- [ ] Confirm `secrets/` has no real files (only `.gitkeep` if needed)
- [ ] Confirm `runs/`, `vault-export/`, `learning_points.txt`, `claude_share_*` are ignored
- [ ] If any key ever touched git history → **rotate at provider immediately**
- [ ] `.env.example` has empty placeholders only (no real values)

### First commit — what to include / exclude

**Never publish:**

- [ ] `backend/.env`
- [ ] `runs/`, `vault-export/`, real files under `secrets/`
- [ ] `learning_points.txt`, `claude_share_*`
- [ ] `hurbor_learn/` (Harbor job artifacts + oracle solutions — gitignored)
- [ ] `frontend/*backup*.html`
- [ ] `.claude/settings.local.json`

**Decide intentionally (include or exclude):**

- [ ] `ideas/` — internal product strategy (Prime, Pi, roadmap). Public OK or keep private?
- [ ] `Framework V1 - Input Flow Design.pdf` — internal design doc?
- [ ] `.claude/launch.json`, `.claude/serve_ui.py` — dev helpers (low risk)

**Safe staging (review before commit):**

```bash
git status
git add .github .gitignore LICENSE README.md SECURITY.md ARCHITECTURE.md CONTRIBUTING.md
git add backend/ frontend/ examples/ docs/ scripts/
git add docker-compose.yml docker-compose.hardened.yml .pre-commit-config.yaml
git diff --cached --stat    # read every path
git commit -m "Initial open-source release"
```

### CI & quality gate

- [ ] Local gate green:
  ```bash
  cd backend && source venv/bin/activate
  pip install -r requirements-dev.txt
  ruff check . && ruff format --check .
  mypy app
  python tests/run_ci.py          # expect 13/13 with Docker
  ```
- [ ] Push to GitHub; confirm `.github/workflows/ci.yml` passes on main
- [ ] (Optional) Enable branch protection: require CI on PRs

---

## P1 — Security (code + ops + user-facing)

TraceTensor **executes untrusted agent code** in containers. Security = sandbox + honest docs.

### Controls already in code

- [x] Agents run in Docker (`environment.py`)
- [x] Default `network_mode = "no-network"` on new tasks (`tasks init`)
- [x] Isolated verifier; `/logs/verifier` not agent-writable
- [x] Guardrails on agent commands (SSH keys, docker.sock, `rm -rf /`, …) — flag mode
- [x] Optional `API_TOKEN` on all `/v1` API routes (`security.py`)
- [x] Optional `RATE_LIMIT_PER_MINUTE` on job creation
- [x] Hardened compose: docker-socket-proxy + secret files (`docker-compose.hardened.yml`)
- [x] `SECURITY.md` threat model at repo root
- [x] Keys via env / `*_FILE`, not hardcoded in source

### Document for users (README / SECURITY)

- [ ] **Localhost:** `API_TOKEN` unset is fine
- [ ] **Internet-facing deploy:** must set `API_TOKEN` + TLS — else anyone can start jobs and spend LLM budget
- [ ] **Default compose mounts Docker socket** — use hardened compose or dedicated Docker host for production
- [ ] **Postgres default password** (`tracetensor/tracetensor`) is dev-only
- [ ] **`network_mode = "public"`** tasks: agents get network; installed agents receive API keys in container
- [ ] **Vault exports** may contain full trajectories — scrub before sharing publicly

### README security callout (recommended before launch)

Add a short box near Quick start:

> **Security:** TraceTensor runs untrusted code in Docker. For localhost dev, no auth is required. If the API is reachable from the internet, set `API_TOKEN` in `backend/.env` and use TLS. See [SECURITY.md](../SECURITY.md).

### Threats we do NOT claim to fully stop (be honest)

- Prompt / task injection (guardrails are tripwires, not a WAF)
- Malicious third-party task bundles
- Docker zero-day container escape (keep Docker patched)
- Multi-tenant SaaS isolation (Phase 1 = self-hosted, trusted operator)

### Optional hardening (nice, not launch-blocking)

- [ ] `CODE_OF_CONDUCT.md`
- [ ] GitHub issue templates / security policy link in repo settings
- [ ] Suggest `RATE_LIMIT_PER_MINUTE=30` in `.env.example` comments
- [ ] Dependabot / dependency scanning (GitHub settings)

---

## P2 — Product & code (Harbor local eval)

### Core flow — already built (do not rebuild for launch)

- [x] Harbor-compatible task format (`instruction.md`, `task.toml`, `environment/`, `tests/`, `solution/`)
- [x] `tracetensor run` — local Docker + optional `--server`
- [x] Multiple trials + concurrency
- [x] Oracle agent
- [x] Installed agents: mini-swe, claude-code, codex (verified ✅)
- [x] LLM loop agents: anthropic / openai / openrouter
- [x] Scoring: `test.sh`, reward.txt/json, Reward Kit (`reward.toml`), llm-judge
- [x] Trajectories saved; Vault CLI (`list`, `show`, `export`)
- [x] `tracetensor tasks init` / `validate`
- [x] Dataset: `dataset run`, `dataset pull` (local, git, swebench slice)
- [x] Web dashboard + `tracetensor serve` + leaderboard
- [x] Agent status labels honest in code + README (gemini/copilot ⏳, openhands ❌)

### Explicitly NOT in this launch (defer)

- [ ] GEPA / self-improve loop
- [ ] Langfuse / production trace ingest
- [ ] Failure → task pipeline
- [ ] Harbor differential golden test (Phase 3 — gates "Harbor-equivalent" claim only)
- [ ] SWE-bench headline number at scale (Phase 2)
- [ ] PyPI publish (GitHub-only launch is OK)
- [ ] Gemini / Copilot verification (keep ⏳ until real run)
- [ ] OpenHands support (keep ❌)
- [ ] Cloud sandboxes (Modal, E2B, …)
- [ ] Harbor Hub / ATIF export / Terminus-2

### Honest claims table

| You CAN say | You CANNOT say (yet) |
|---|---|
| Self-hosted coding agent eval in Docker | "Harbor-equivalent" (needs Phase 3 diff test) |
| Harbor-compatible task format | "Every agent verified" (gemini/copilot/openhands) |
| Scores, trajectories, local Vault | "Production multi-tenant SaaS" |
| Compare models / agents on same task | "Full Harbor Hub / cloud parity" |
| Open source, Apache-2.0 | "RL training platform" |

---

## P3 — Documentation & onboarding

- [ ] README quickstart works on a **fresh machine**: clone → venv → pip install → one `tracetensor run`
- [ ] README lists honest agent ✅ / ⏳ / ❌ table (matches `agent_status_catalog`)
- [ ] README explains Vault split: local CLI `runs/` vs dashboard Postgres (when using `--server`)
- [ ] `CONTRIBUTING.md` — dev setup + test tiers
- [ ] `SECURITY.md` — linked from README
- [ ] `LICENSE` present (Apache-2.0)
- [ ] Examples run: `sort-csv`, `fix-add`, `pr-fix-duration` (oracle at minimum)

---

## P4 — Smoke tests (before flip public)

Run on the machine you will demo from (and ideally a second clean clone):

```bash
# Free — no API key
tracetensor run ../examples/sort-csv -a oracle -n 1
tracetensor tasks validate ../examples/fix-add

# Real agent — needs OPENAI_API_KEY or ANTHROPIC_API_KEY
tracetensor run ../examples/fix-add -a openai -m gpt-4.1-mini -n 1

# Vault
tracetensor vault list
tracetensor vault show <run-id>

# Server + UI (optional)
tracetensor serve
# open http://localhost:8000 — try example task
```

- [ ] Oracle run passes
- [ ] At least one real-agent run passes
- [ ] Vault shows the run
- [ ] (Optional) UI run completes and appears in dashboard Vault

---

## P5 — GitHub repo settings (after first push)

- [ ] Repo description + topics (e.g. `agent-evaluation`, `docker`, `harbor`, `llm`)
- [ ] Default branch: `main`
- [ ] Add SECURITY.md link in GitHub **Security → Advisories**
- [ ] Disable wiki if unused
- [ ] (Optional) Release tag `v0.1.0` with short release notes
- [ ] (Optional) Pin README badge for CI status after first green run

---

## P6 — Friday timeline

| When | Task | Done |
|---|---|---|
| **Thu PM** | Secrets scan + `git status` review | [ ] |
| **Thu PM** | Fix any `.gitignore` gaps | [x] `hurbor_learn/` added |
| **Thu PM** | README security callout + quickstart pass | [ ] |
| **Thu PM** | First commit → push (private repo first recommended) | [ ] |
| **Thu PM** | CI green on GitHub | [ ] |
| **Fri early** | Fresh-clone smoke test | [ ] |
| **Fri < 8 AM** | Make repo public / announce | [ ] |

---

## Quick reference commands

```bash
# --- Pre-push safety ---
git status
git check-ignore -v backend/.env hurbor_learn/jobs runs/ secrets/
rg -n 'sk-(ant|proj)-' --glob '!.git' --glob '!backend/.env' .

# --- Quality gate ---
cd backend && source venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && mypy app
python tests/run_ci.py

# --- Pre-commit (optional local hook) ---
pre-commit run --all-files
```

---

## Post-launch (not blocking Friday)

Track in [LAUNCH_PLAN.md](LAUNCH_PLAN.md) and [OSS_ROADMAP.md](OSS_ROADMAP.md):

- Verify gemini / copilot agents with real keys
- Harbor differential test (10 tasks) if claiming equivalence
- SWE-bench slice at scale + publish number
- PyPI publish (`pip install tracetensor`)
- GEPA / improve loop, Langfuse, benchmark adapters (Phase 2)

---

## Audit log

| Date | Notes |
|---|---|
| 2026-08-05 | Full repo audit: CI 13/13 green; real keys in `backend/.env` (gitignored); `hurbor_learn/` added to `.gitignore`; no git commits yet |
