# Pre-launch audit — ship by Friday 8 AM

Generated from a full repo review. Use this as the **must-do** list before the
first public `git push`.

---

## Verdict (short)

**The product code is ready to open source** for Harbor-style local eval
(task → sandbox → agent → score → Vault). CI passes **13/13** suites.

**What can still burn you:** accidentally committing secrets or internal junk,
and users deploying the API without `API_TOKEN` on the public internet.

---

## P0 — BLOCKERS (do before any push)

### 1. Secrets — never commit real keys

- `backend/.env` contains **real API keys** (Anthropic, OpenAI). It is
  **gitignored** — do **not** `git add -f backend/.env`.
- Run before staging:
  ```bash
  git status
  git check-ignore -v backend/.env
  rg -n 'sk-(ant|proj)-' --glob '!.git' --glob '!backend/.env' .
  ```
- If keys were ever committed in history → **rotate them immediately** at the
  provider dashboards.

### 2. First commit — stage deliberately

**No commits exist yet.** A blind `git add .` is dangerous.

**Do NOT publish:**
- `backend/.env`
- `runs/`, `vault-export/`, `secrets/*` (except empty `.gitkeep`)
- `learning_points.txt`, `claude_share_*`
- `hurbor_learn/` (Harbor job artifacts + oracle text — now gitignored)
- `frontend/*backup*.html`

**Decide before push:**
- `ideas/` — internal product strategy (Prime, Pi). OK public or move to private notes.
- `Framework V1 - Input Flow Design.pdf` — internal PDF; exclude unless intentional.
- `.claude/launch.json`, `.claude/serve_ui.py` — dev helpers; harmless but optional.

**Safe staging pattern:**
```bash
git add .github .gitignore LICENSE README.md SECURITY.md ARCHITECTURE.md CONTRIBUTING.md
git add backend/ frontend/ examples/ docs/ scripts/
git add docker-compose.yml docker-compose.hardened.yml .pre-commit-config.yaml
# Review: git diff --cached --stat
```

### 3. Confirm CI green on GitHub

Local: `cd backend && pip install -r requirements-dev.txt && python tests/run_ci.py`
→ **13/13 passed** (with Docker).

After push: watch `.github/workflows/ci.yml` on the first PR/main push.

---

## P1 — Security (what hackers / misusers can do)

TraceTensor **runs untrusted agent code**. Treat that as the core threat.

### What you already have (good)

| Control | Where |
|---|---|
| Agents run in Docker containers | `environment.py` |
| Default `no-network` per task | `task.toml` / validator |
| Verifier in isolated path; `/logs/verifier` not agent-writable | `verifier.py`, `environment.py` |
| Output guardrails (SSH keys, docker.sock, rm -rf /) | `guardrails.py` |
| Optional `API_TOKEN` on all API routes | `security.py`, `main.py` |
| Optional rate limit on job creation | `RATE_LIMIT_PER_MINUTE` |
| Hardened compose: docker-socket-proxy + secrets files | `docker-compose.hardened.yml` |
| Threat model doc | `SECURITY.md` |
| Keys via env / `*_FILE`, not in repo | `config.py`, `.env.example` |

### What users MUST be told (README / SECURITY)

1. **Localhost dev:** `API_TOKEN` unset is fine.
2. **Any server reachable from the internet:** set `API_TOKEN` + TLS — otherwise
   anyone can start jobs and **spend your LLM budget**.
3. **Default `docker-compose.yml` mounts Docker socket** — if the API is
   compromised, attacker gets Docker-level host access. For production use
   `docker-compose.hardened.yml` or a dedicated Docker host.
4. **Default Postgres password** (`tracetensor/tracetensor`) is **dev only**.
5. **Tasks with `network_mode = "public"`** — installed agents can reach the
   internet; API keys are injected into the container for those agents.
6. **Vault exports** may contain prompts, command output, and occasionally
   secrets the agent printed — scrub before sharing.

### What you are NOT protecting against (be honest)

- Prompt injection inside tasks (research problem; guardrails are tripwires only).
- Malicious task authors (treat third-party tasks as untrusted).
- Container escape bugs in Docker itself (keep Docker patched).
- Multi-tenant isolation at scale (Phase 1 = self-hosted, trusted operator).

### Optional hardening before launch (nice, not required)

- [ ] README warning box: "Before exposing to the internet, set API_TOKEN"
- [ ] Default `RATE_LIMIT_PER_MINUTE=30` in `.env.example` comment
- [ ] `CODE_OF_CONDUCT.md` (GitHub community standard)

---

## P2 — Code / product (Harbor local eval)

### Already shipped (don't rebuild)

- Harbor-compatible task format
- `tracetensor run` (local + `--server`)
- Oracle + installed agents (mini-swe, claude-code, codex verified)
- Scoring: test.sh, reward.txt/json, reward.toml, llm-judge
- Trajectories + Vault CLI
- Dataset run + pull (local, git, swebench slice)
- Web dashboard + leaderboard
- `tracetensor tasks init` / `validate`

### Not required for Friday OSS (defer)

- GEPA / improve loop
- Langfuse / prod traces
- Harbor differential golden test (Phase 3 — only for "Harbor-equivalent" claim)
- SWE-bench headline number (Phase 2)
- PyPI publish (can ship GitHub-only first)
- Gemini / Copilot real verification (keep ⏳ labels)
- OpenHands (keep ❌)

### Honest public claim

✅ "Self-hosted coding agent eval in Docker — Harbor-compatible tasks, scores, trajectories, Vault."

❌ "Harbor-equivalent" (until differential test passes).

❌ "Production-hardened multi-tenant SaaS."

---

## P3 — Friday morning timeline (suggested)

| When | Task |
|---|---|
| **Thu PM** | Secrets scan + fix `.gitignore` (hurbor_learn done) |
| **Thu PM** | First commit + push to private repo; CI green |
| **Thu PM** | README quickstart: clone → install → one `tracetensor run` |
| **Fri early** | Flip repo public OR publish release tag |
| **Fri < 8 AM** | Smoke test: fresh clone on clean machine (or friend) |

---

## Quick commands (copy-paste)

```bash
# Pre-push safety
git status
git check-ignore -v backend/.env hurbor_learn/jobs runs/
rg -n 'sk-(ant|proj)-' --glob '!.git' --glob '!backend/.env' .

# Quality gate
cd backend && source venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && mypy app && python tests/run_ci.py

# Smoke (needs Docker + one API key)
tracetensor run ../examples/sort-csv -a oracle -n 1
tracetensor run ../examples/fix-add -a openai -m gpt-4.1-mini -n 1
tracetensor vault list
```

---

## Related docs

- [SECRETS_AUDIT.md](SECRETS_AUDIT.md)
- [LAUNCH_PLAN.md](LAUNCH_PLAN.md)
- [SECURITY.md](../SECURITY.md)
- [OSS_ROADMAP.md](OSS_ROADMAP.md)
