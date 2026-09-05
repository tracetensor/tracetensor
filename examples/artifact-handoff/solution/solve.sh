#!/bin/bash
# Oracle reference. The agent's job here is trivial on purpose — what is being
# tested is whether the backend can move this file into a separate grading
# sandbox, not whether a model can write it.
set -e
mkdir -p /app/out
printf 'handoff-ok\n' > /app/out/result.txt
