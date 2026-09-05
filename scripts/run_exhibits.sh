#!/usr/bin/env bash
# Fire the nine exhibit runs. Every run: 1 trial, real agent, Daytona sandbox.
# Failures are expected on 04 and are NOT errors — they are the exhibit.
# Each run writes its own artifacts to runs-exhibits/ regardless of outcome.
set -u

cd "$(dirname "$0")/.." || exit 1
TT=backend/.venv/bin/tracetensor
OUT=runs-exhibits
LOG=$OUT/_run.log
mkdir -p "$OUT"

run () {                      # run <label> <task> <agent> [model]
  local label=$1 task=$2 agent=$3 model=${4:-}
  local args=(run "tasks/exhibits/$task" -a "$agent" -n 1 --backend daytona -o "$OUT")
  [ -n "$model" ] && args+=(-m "$model")

  echo "" | tee -a "$LOG"
  echo "════ $label · $task · $agent ${model:+/ $model}" | tee -a "$LOG"
  date -u '+start %Y-%m-%dT%H:%M:%SZ' | tee -a "$LOG"

  "$TT" "${args[@]}" >>"$LOG" 2>&1
  local rc=$?
  # rc!=0 usually means the agent did not pass — still a valid recorded run
  echo "exit=$rc" | tee -a "$LOG"
  date -u '+end   %Y-%m-%dT%H:%M:%SZ' | tee -a "$LOG"
}

echo "=== exhibit runs started $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" | tee "$LOG"

# 01 · on-call triage from raw logs
run "01/9" 01-triage-from-logs  claude-code haiku

# 02 · model cost gate — same task, two model tiers
run "02/9" 02-model-cost-gate   claude-code haiku
run "03/9" 02-model-cost-gate   claude-code sonnet

# 03 · harness bake-off — one bug, three agents
run "04/9" 03-harness-bakeoff   claude-code haiku
run "05/9" 03-harness-bakeoff   codex
run "06/9" 03-harness-bakeoff   mini-swe

# 04 · task or model — two real agents on a hard task
run "07/9" 04-task-or-model     claude-code sonnet
run "08/9" 04-task-or-model     codex

# 05 · audit trail
run "09/9" 05-audit-trail       claude-code sonnet

echo "" | tee -a "$LOG"
echo "=== all runs finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" | tee -a "$LOG"
ls -1 "$OUT" | grep -v '^_' | tee -a "$LOG"
