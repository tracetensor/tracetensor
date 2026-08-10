#!/bin/bash
# Two independent, named criteria — NOT a single "reward" key — so the verifier
# must use the Reward Kit spec (reward.toml) to aggregate them into a scalar.
mkdir -p /logs/verifier
CONTENT="$(cat /app/output.txt 2>/dev/null || echo '')"
LEN=${#CONTENT}

CORRECT=0.0
echo "$CONTENT" | grep -qi "correct" && CORRECT=1.0

BREVITY=0.0
[ "$LEN" -lt 50 ] && BREVITY=1.0

cat > /logs/verifier/reward.json <<EOF
{"correctness": $CORRECT, "brevity": $BREVITY}
EOF
cat /logs/verifier/reward.json
