#!/bin/bash
mkdir -p /logs/verifier
if python3 /tests/test_sort.py; then
    echo '{"sorted_correctly": 1.0}' > /logs/verifier/reward.json
else
    echo '{"sorted_correctly": 0.0}' > /logs/verifier/reward.json
fi
