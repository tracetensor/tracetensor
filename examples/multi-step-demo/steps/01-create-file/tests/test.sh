#!/bin/sh
set -e

if [ -f /workspace/hello.txt ] && grep -qx "hello" /workspace/hello.txt; then
    echo "REWARD=1.0"
    exit 0
else
    echo "hello.txt missing or wrong content" >&2
    echo "REWARD=0.0"
    exit 1
fi
