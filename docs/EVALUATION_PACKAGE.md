# TraceTensor Evaluation Package

**One public input style.** Developers ship a complete folder with plain names.
TraceTensor maps it 1:1 into Harbor and runs the existing execution kernel.

Harbor remains the runtime (sandboxes, trials, verifiers). TraceTensor improves
terminology and intake validation — it does **not** dig through your monorepo
to invent tasks.

## Layout

```text
my-evaluation/
├── prompt.md                 # required — what the agent should do
├── settings.toml             # required — name, timeouts, limits (Harbor task.toml schema)
├── runtime/                  # required — how it runs
│   └── Dockerfile            # required unless settings sets docker_image
│   └── …                     # code, fixtures, deps
├── checks/                   # required — pass / fail
│   └── check.sh              # required entry script
├── sample/                   # recommended — one real input example
└── expected/                 # optional — known-good reference (oracle)
    └── solve.sh
```

## Mapping to Harbor (internal)

| TraceTensor | Harbor |
|---|---|
| `prompt.md` | `instruction.md` |
| `settings.toml` | `task.toml` |
| `runtime/` | `environment/` |
| `checks/check.sh` | `tests/test.sh` |
| `expected/` | `solution/` |
| `sample/` | `environment/sample/` |

Classic Harbor zips (`task.toml` + `instruction.md` + …) still upload and run.
That is silent compatibility — not a second product flow.

## CLI

```bash
tracetensor tasks validate ../examples/hello-eval
tracetensor run ../examples/hello-eval -a oracle
```

Upload the same folder as a zip via `POST /ingest/task/upload`.

## Phase 2 (not built yet)

A short guided **wizard** that helps the developer *write* these files
(entrypoint, sample input, success rule) — not an explorer that searches a
large repo. Same package + Harbor path once the files exist.

## What we will not do as default intake

- Agentic whole-repo discovery (`workspace evaluate` is hidden / advanced only)
- Auto-inventing ground truth or oracles from vague codebases
- Dual “Workspace vs Harbor” onboarding flows
