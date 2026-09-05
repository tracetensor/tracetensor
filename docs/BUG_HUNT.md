# Bug hunt playbook

How to run tasks repeatedly, classify failures, and fix the right layer before launch.

Related: [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md) · [LAUNCH_PLAN.md](LAUNCH_PLAN.md)

---

## Daily gates (in order)

### Gate 1 — Oracle (free, ~1 min)

Proves each task + Docker + verifier works. Catches broken tasks like unfair tests.

```bash
cd backend && source venv/bin/activate
tracetensor dataset run ../examples/test-suite -a oracle
tracetensor dataset run ../examples -a oracle   # original examples too
```

**Must be 10/10** on test-suite. CI runs this via `backend/scripts/oracle_test_suite.py`.

### Gate 2 — Cheap agent smoke (paid, ~10–20 min)

One trial per task with a fast model:

```bash
tracetensor dataset run ../examples/test-suite -a openai -m gpt-5.4-nano -n 1
```

Log failures — don't fix the model, fix harness or task.

### Gate 3 — Hard tasks with installed agents

Multi-file bugfix tasks need codex / claude-code / mini-swe, not `-a openai`:

```bash
tracetensor run ../examples/test-suite/09-fix-checkout -a codex -m gpt-5.4-mini -n 1
tracetensor run ../examples/fix-add -a codex -n 1
```

### Gate 4 — Stress batch (optional)

15-trial random mix across models:

```bash
python backend/scripts/run_test_suite_batch.py
```

Report written to `backend/runs/test-suite-batch-report.json`.

---

## Classify every failure

| Type | Symptom | Fix |
|------|---------|-----|
| **Harness** | Crash, no score, Docker error, API key not loaded | TraceTensor code |
| **Task** | Oracle fails, or agent output correct but verifier fails | `instruction.md` / `tests/` |
| **Agent choice** | Multi-file edit with `-a openai`, 0 bash steps | Use codex / claude-code; see README |
| **Model** | Reasonable agent work, wrong answer | Log it; not a platform bug |

---

## Debugging a failed trial

1. CLI now prints **failure hints** under the trials table.
2. Use `--verbose` for structured JSON logs: `tracetensor run … --verbose`
3. Artifacts: `backend/runs/*/result.json` — full trajectory + verifier stderr.
4. Look for `agent_error` and `warnings` in trajectory.

---

## Fresh-clone smoke (pre-launch)

On a clean machine:

```bash
git clone … && cd tracetensor/backend
python3 -m venv venv && source venv/bin/activate
pip install -e .
tracetensor tasks init /tmp/my-task
tracetensor run /tmp/my-task -a oracle
```

If that works, a new user can run evaluations locally.

---

## What CI covers

- `python backend/tests/run_ci.py` — offline + Docker oracle suites
- `python backend/scripts/oracle_test_suite.py` — all 10 test-suite tasks oracle
- Real-LLM e2e — optional when `ANTHROPIC_API_KEY` secret is set
