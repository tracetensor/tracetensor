#!/bin/sh
set -e

expected="hello
world"

actual=$(cat /workspace/hello.txt 2>/dev/null || echo "")

if [ "$actual" = "$expected" ]; then
    echo "REWARD=1.0"
    exit 0
else
    echo "Expected:" >&2
    printf '%s\n' "$expected" >&2
    echo "Got:" >&2
    printf '%s\n' "$actual" >&2
    echo "REWARD=0.0"
    exit 1
fi
