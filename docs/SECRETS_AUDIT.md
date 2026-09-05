# Secrets audit checklist (before first public commit)

Run through this once before `git add` / first push.

1. **Never stage**
   - `backend/.env`, any `.env` with real keys
   - `secrets/*` (except `.gitkeep`)
   - `claude_share_*`, `.scratch/`, `runs/`, `vault-export/`
   - `learning_points.txt`, `frontend/*backup*.html`

2. **Confirm ignores** — `.gitignore` covers the paths above; `.claude/settings.local.json`
   is ignored (local Claude mode should be `acceptEdits`, not `bypassPermissions`).

3. **Template only** — `backend/.env.example` lists every key name with empty values
   (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`,
   `GH_TOKEN` / `COPILOT_GITHUB_TOKEN` / `GITHUB_TOKEN`, `API_TOKEN`, DB URL, …).

4. **Quick scan** (from repo root, after `git init`):
   ```bash
   git status
   git check-ignore -v backend/.env claude_share_dummy.json learning_points.txt
   # optional: rg -n 'sk-|ANTHROPIC_API_KEY=.+' --glob '!.git' --glob '!venv' .
   ```

5. **SECURITY.md** — threat model + Vault export warning already at repo root.
