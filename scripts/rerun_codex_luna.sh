#!/usr/bin/env bash
# Re-run the two codex exhibits on gpt-5.6-luna.
#
# The registry default (gpt-5-codex) 404s on /v1/responses for this account, so
# both codex runs recorded reward 0.000 without ever reaching a model. That is a
# configuration failure, not an agent failing a task — the artifacts are deleted
# rather than published, and the runs are repeated on a model that resolves.
set -u

cd "$(dirname "$0")/.." || exit 1
TT=backend/.venv/bin/tracetensor
OUT=runs-exhibits
LOG=$OUT/_run.log
MODEL=gpt-5.6-luna

# 1. wait for the main batch to finish
while pgrep -f run_exhibits.sh >/dev/null 2>&1; do sleep 10; done

echo "" | tee -a "$LOG"
echo "=== codex re-runs on $MODEL $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" | tee -a "$LOG"

# 2. drop the 404 artifacts so they can never be mistaken for real failures
for d in "$OUT"/*/; do
  [ -f "$d/result.json" ] || continue
  python3 - "$d" <<'PY' || continue
import json,sys,shutil,pathlib
d=pathlib.Path(sys.argv[1]); r=json.load(open(d/'result.json'))
u=r['trials'][0]['trajectory']['llm_usage_summary']
if r.get('agent')=='codex' and not u.get('cost_usd'):
    shutil.rmtree(d); print(f"removed 404 artifact: {d.name}")
    sys.exit(0)
sys.exit(1)
PY
done | tee -a "$LOG"

# 3. repeat both codex runs on a model that resolves
run () {
  local label=$1 task=$2
  echo "" | tee -a "$LOG"
  echo "════ $label · $task · codex / $MODEL" | tee -a "$LOG"
  date -u '+start %Y-%m-%dT%H:%M:%SZ' | tee -a "$LOG"
  "$TT" run "tasks/exhibits/$task" -a codex -m "$MODEL" -n 1 --backend daytona -o "$OUT" >>"$LOG" 2>&1
  echo "exit=$?" | tee -a "$LOG"
}

run "R1/2" 03-harness-bakeoff
run "R2/2" 04-task-or-model

echo "" | tee -a "$LOG"
echo "=== codex re-runs finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ===" | tee -a "$LOG"
