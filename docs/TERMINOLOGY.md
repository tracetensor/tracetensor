# TraceTensor terminology (canonical)

**Status:** locked — **Harbor vocabulary everywhere** (on disk, CLI, dashboard, website, docs).

TraceTensor is Harbor-compatible. We use **the same words Harbor uses** in every layer. No alternate product rename (no “evaluation / run / score” in user-facing copy).

---

## Vocabulary (one table)

| Concept | Term | On disk | JSON / API |
|---|---|---|---|
| Scorable folder | **task** | folder + `task.toml` | `"task": "fix-add"` |
| What the agent reads | **instruction** | `instruction.md` | — |
| Docker sandbox | **environment** | `environment/` | — |
| Grading script | **tests** | `tests/test.sh` | — |
| Grading step (runtime) | **verifier** | `/logs/verifier/reward.json` | phase `"verifier"` |
| Reference fix | **solution** · **oracle** | `solution/` | — |
| One agent attempt | **trial** | — | `"trials": [...]`, `"n_trials": 3` |
| Pass/fail number | **reward** | `reward.json` / `reward.txt` | `"reward": 1.0`, `"mean_reward"` |
| Who acts | **agent** | — | `"agent": "mini-swe"` |
| Step-by-step log | **trajectory** | under `runs/` | in `trials[]` |
| Saved artifacts | **runs/** · **Vault** | `runs/*/result.json` | — |
| Batch of tasks | **dataset** | — | — |
| Server job | **job** | — | job id |

**One sentence:** Register a **task**, run **trials** with an **agent**, the **verifier** writes **reward**, browse **trajectories** in the **Vault**.

---

## Task folder layout (Harbor-exact)

```text
my-task/
├── instruction.md
├── task.toml
├── environment/
├── tests/
└── solution/          # optional
```

---

## CLI (Harbor-aligned)

| Command | Meaning |
|---|---|
| `tracetensor run <task>` | Run agent trials on one task |
| `tracetensor tasks validate <task>` | Check task is runnable |
| `tracetensor tasks init ./my-task` | Scaffold Harbor layout |
| `tracetensor dataset run <dir>` | Run agent on every task in a dataset |
| `tracetensor vault list` | Browse saved trial results |
| `-n / --n-trials` | Number of trials per task |

Machine output (`--json`, `result.json`) uses Harbor field names: `task`, `n_trials`, `trials`, `reward`, `mean_reward`.

---

## Trial lifecycle (Harbor phases)

```text
parse → setup → agent → verify (verifier) → reward → runs/
```

| Code phase | Label |
|---|---|
| `setup` | Setup |
| `agent` | Agent |
| `verify` | Verify (verifier) |
| `score` | Result (reward) |

---

## Prime Intellect mapping (positioning only)

When comparing to Prime Intellect docs, Harbor/TraceTensor terms map as:

| Prime | Harbor / TraceTensor |
|---|---|
| taskset | task (instruction + tests + `task.toml`) |
| harness | agent |
| runtime | Docker backend |
| rubric / reward | verifier / reward |
| rollout | trial |

---

## Related docs

- [HARBOR_TT_MAPPING.md](1-1-archi-learning/HARBOR_TT_MAPPING.md) — architecture + code map
- [ideas/environments-and-positioning.md](../ideas/environments-and-positioning.md) — Prime positioning
