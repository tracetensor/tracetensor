<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
</p>

# TraceTensor

Self-hosted evaluation platform for coding agents. Run any agent (Claude Code, Codex, mini-swe, or your own) against any task, inside isolated Docker sandboxes, and get reproducible pass/fail scores.

**No cloud dependency. No data leaves your machine. You own the results.**

## Quickstart

```bash
# 1. Clone and configure
git clone https://github.com/tracetensor/tracetensor.git
cd tracetensor
cp .env.example .env
# Edit .env — add at least one LLM API key (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)

# 2. Start the stack
docker compose up -d

# 3. Install the CLI
pip install -e backend/

# 4. Run your first evaluation
tracetensor run examples/fix-add -a oracle
```

You should see:

```
Trial  Result  Reward  Time
   #1  ✓ pass   1.000  1.4s

╭───────── result ─────────╮
│  pass rate  1/1  (100%)  │
│  mean reward 1.000       │
╰──────────────────────────╯
```

## Run a real agent

```bash
# Claude Code with Sonnet
tracetensor run examples/fix-add -a claude-code -m claude-sonnet-4-6 -n 3

# Run all tasks in a dataset
tracetensor dataset run examples/test-suite -a claude-code -m claude-sonnet-4-6
```

## Supported agents

| Agent | Type | Required key |
|-------|------|-------------|
| `oracle` | Runs `solution/solve.sh` (free, for testing) | None |
| `claude-code` | Anthropic Claude Code CLI | `ANTHROPIC_API_KEY` |
| `codex` | OpenAI Codex CLI | `OPENAI_API_KEY` |
| `mini-swe` | SWE-agent (pip-installed) | `ANTHROPIC_API_KEY` |
| `anthropic` | Anthropic API via litellm | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI API via litellm | `OPENAI_API_KEY` |
| `openrouter` | OpenRouter API via litellm | `OPENROUTER_API_KEY` |

## Task format

A task is a directory with this layout:

```
my-task/
  instruction.md          # What the agent should do
  task.toml               # Metadata, timeouts, environment config
  environment/
    Dockerfile            # Container the agent works in
  tests/
    test.sh               # Verifier — writes reward.json
  solution/
    solve.sh              # (Optional) Reference solution for oracle
```

**task.toml** example:

```toml
schema_version = "1.3"

[task]
name = "my-org/fix-the-bug"
description = "Fix a one-line bug in add.py"

[environment]
network_mode = "public"

[agent]
timeout_sec = 400.0

[verifier]
timeout_sec = 60.0
```

**test.sh** must write a JSON file with a `reward` key:

```bash
#!/bin/bash
if python3 /tests/test_add.py; then
    echo '{"reward": 1.0}' > /logs/verifier/reward.json
else
    echo '{"reward": 0.0}' > /logs/verifier/reward.json
fi
```

Scaffold a new task:

```bash
tracetensor tasks init my-task
```

## CLI reference

```
tracetensor run <task> -a <agent> -m <model> -n <N>   Run trials + verifier
tracetensor serve                                     Start the API server
tracetensor tasks init <dir>                          Scaffold a new task
tracetensor tasks validate <task>                     Check a task is runnable
tracetensor dataset run <dir> -a <agent>              Run all tasks in a dataset
tracetensor vault list                                Browse saved results
tracetensor vault show <run>                          Inspect a specific run
tracetensor vault export <run> -o <path>              Export run artifacts
tracetensor version                                   Show installed version
```

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  CLI / API                                           │
│  tracetensor run ... ──► executor ──► trial_runner   │
│                                        │             │
│                           ┌────────────┼──────────┐  │
│                           │  Docker container     │  │
│                           │  ┌─────────────────┐  │  │
│                           │  │  Agent runs here │  │  │
│                           │  │  (sandboxed)     │  │  │
│                           │  └─────────────────┘  │  │
│                           │  ┌─────────────────┐  │  │
│                           │  │  Verifier grades │  │  │
│                           │  │  (test.sh)       │  │  │
│                           │  └─────────────────┘  │  │
│                           └───────────────────────┘  │
│                                        │             │
│                           results ◄────┘             │
│                           (pass/fail, reward, cost)  │
└──────────────────────────────────────────────────────┘
```

Each trial runs in its own container. The agent and verifier never share state. Results are deterministic and reproducible.

## Scaling

Single-machine by default. For horizontal scaling:

```bash
# Run dedicated workers (share the same Postgres queue)
WORKER_EMBEDDED=false docker compose --profile scale up --scale worker=4
```

## Examples

The `examples/` directory includes ready-to-run tasks:

- **fix-add** — one-line Python bug (great for testing your setup)
- **test-suite/** — 10 tasks of increasing difficulty
- **pr-suite/** — 5 real-world PR-style fixes
- **mediaos-deep-v2/** — 3 complex multi-file tasks
- **agentic-suite/** — 5 tasks requiring multi-step reasoning

Run them all:

```bash
tracetensor dataset run examples/test-suite -a oracle
```

## Task format interoperability

TraceTensor uses the same task folder convention (`instruction.md`, `task.toml`, `tests/test.sh`) established by the open evaluation community. Tasks written for other harnesses that follow this layout can run on TraceTensor without modification.

## License

Apache 2.0 — see [LICENSE](LICENSE).
