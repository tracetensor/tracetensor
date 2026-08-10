#!/bin/bash
mkdir -p /logs/verifier
if python3 /tests/test_phone.py; then
    echo '{"reward": 1.0}' > /logs/verifier/reward.json
else
    echo '{"reward": 0.0}' > /logs/verifier/reward.json
fi
