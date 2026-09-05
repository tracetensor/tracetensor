#!/bin/bash
# Runs inside the SEPARATE grading sandbox. The only thing here from the agent's
# run is what the backend transferred, so this failing means either the agent
# didn't produce the artifact or the handoff didn't work.
mkdir -p /logs/verifier
if [ -f /app/out/result.txt ] && grep -qx 'handoff-ok' /app/out/result.txt; then
    echo '{"reward": 1.0}' > /logs/verifier/reward.json
    echo "PASS  artifact arrived in the grading sandbox"
else
    echo '{"reward": 0.0}' > /logs/verifier/reward.json
    echo "FAIL  /app/out/result.txt missing or wrong"
    ls -la /app/out 2>&1 || echo "      /app/out does not exist here"
fi
