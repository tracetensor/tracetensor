# Harbor → TraceTensor: 1:1 Architecture Learning

Learning notes from Harbor’s subsystem map
([DeepWiki overview](https://deepwiki.com/harbor-framework/harbor/1-overview))
and how TraceTensor maps onto it.

**Product stance (locked):**

- Harbor = execution kernel (task format, sandbox, agent, verify, score).
- TraceTensor = DX + product language on top.
- Input files stay Harbor-exact.
- Product language: **evaluation** / **run** / **score**.
- Keep Harbor names that are already clear: **instruction**, **environment**, **tests**, **solution**.

---

## 1. Harbor architecture (learned)

### End-to-end flow

```text
CLI / Job
  → TrialQueue
    → Trial Lifecycle
         ├─ Environment  (Docker / Daytona / Modal / …)
         ├─ Agent        (Terminus2 / installed / Oracle)
         └─ Verifier     (tests/test.sh → reward.txt | reward.json)
              → TrialResult + trajectory.json (ATIF)
                   → Harbor Viewer
```

### Subsystem map → Harbor code

| Subsystem | Role | Harbor entities |
|---|---|---|
| **Task & Dataset** | What to evaluate | `RegistryClient`, `TaskClient`, `Task` model (`src/harbor/models/task/task.py`) |
| **Execution** | Orchestration | CLI → `Job` → `TrialQueue` → `Trial` lifecycle |
| **Environment** | Where it runs | `EnvironmentFactory` → Docker, Daytona, Modal, E2B, GKE, Novita, … |
| **Agent** | Who acts | `AgentFactory` → Terminus2, `BaseInstalledAgent`, `OracleAgent` |
| **Verification** | How it scores | `Verifier` runs `test.sh`, reads `reward.txt` / `reward.json` |
| **Results** | What you keep | `TrialResult`, ATIF `trajectory.json`, Harbor Viewer |

### Harbor task input (exact)

```text
<task-dir>/
├── task.toml            # metadata, timeouts, resources
├── instruction.md       # what the agent should do
├── environment/         # Dockerfile (or image)
│   └── Dockerfile
├── tests/               # scoring
│   └── test.sh          # writes reward.txt / reward.json
└── solution/            # optional reference
    └── solve.sh
```

### Harbor outputs (per trial)

| Artifact | Meaning |
|---|---|
| `result.json` | Trial / job metadata + stats |
| `trajectory.json` | Agent interaction history (ATIF) |
| `reward.txt` / `reward.json` | Score from verifier |

### Sources

- [Harbor overview (DeepWiki)](https://deepwiki.com/harbor-framework/harbor/1-overview)
- Harbor subsystem diagrams (Task/Dataset → Execution → Environment / Agent / Verifier → Viewer)

---

## 2. TraceTensor ↔ Harbor (1:1 mapping)

```text
Harbor                         TraceTensor
────────────────────────────────────────────────────────────────
Task Model / task.toml    →    backend/app/schemas/task.py
                               backend/app/models/task.py
                               backend/app/services/task_parser.py

TaskClient / Registry     →    backend/app/routers/ingest.py
                               backend/app/storage/task_store.py
                               backend/app/services/task_validator.py
                               (+ datasets via routers/datasets.py)

CLI                       →    backend/app/cli/main.py
                               (run, serve, tasks, dataset)

Job                       →    backend/app/models/job.py

TrialQueue                →    backend/app/services/job_queue.py
                               backend/app/worker.py
                               (Postgres durable queue — renamed)

Trial Lifecycle           →    backend/app/services/trial_runner.py
                               (setup → agent → verify → score)

EnvironmentFactory        →    make_environment() in
                               backend/app/services/environment.py

DockerEnvironment         →    DockerEnvironment (+ PodmanEnvironment)

AgentFactory              →    make_agent() in
                               backend/app/services/agent.py

OracleAgent               →    OracleAgent (runs solution/solve.sh)

Installed agents          →    mini-swe, claude-code, codex,
                               gemini, copilot, openhands

Built-in LLM agent        →    LLMAgent bash loop (+ llm.py)
                               (TraceTensor-specific)

Verifier                  →    backend/app/services/verifier.py
                               (+ llm_judge.py, Reward Kit)

TrialResult + trajectory  →    TrialOutcome + Trial ORM
                               + trajectory JSON on Trial

Harbor Viewer             →    frontend/index.html
                               (+ SSE via examine router)
```

### By layer — status

| Harbor layer | TraceTensor | Status |
|---|---|---|
| **Task & Dataset** | Harbor folder intake, validator, datasets ZIP/git/swebench | Solid |
| **Execution** | CLI → Job → Postgres queue → `run_trial` | Solid |
| **Environment** | Docker (+ Podman) | Solid locally |
| **Agent** | Oracle + installed + built-in LLM loop | Solid (no Terminus-2) |
| **Verification** | `test.sh` / reward / Reward Kit / llm-judge / separate mode | Solid |
| **Results** | Trajectories, leaderboard, dashboard | Solid functionally |

### Product nomenclature (UI / docs — not on-disk files)

| Harbor / internal | TraceTensor says | On disk |
|---|---|---|
| task | **evaluation** | same Harbor folder |
| trial | **run** | — |
| reward | **score** | `reward.txt` still produced by tests |
| instruction | instruction (keep) | `instruction.md` |
| environment | environment (keep) | `environment/` |
| tests / verifier | tests (keep); avoid “verifier” in UI | `tests/` |
| solution / oracle | solution (keep); oracle = agent that runs it | `solution/` |

---

## 3. Gaps vs Harbor

What Harbor has that TraceTensor does **not** (or only partially):

| Gap | Notes |
|---|---|
| **Terminus-2** | Not shipped; Harbor’s built-in interactive agent. Installed-agent adapters only for now. |
| **Cloud environments** | Daytona, Modal, E2B, GKE, Novita, Runloop, Beam, etc. — missing. Docker/Podman only. |
| **Harbor Hub** | No publish/sync to `hub.harborframework.com`. Local DB + ZIP/git/`swebench` pull only. |
| **`allowlist` network** | Rejected until an egress-control sidecar exists. |
| **LocalEnvironment** | Mentioned in docs/code comments; not a real registered backend. |
| **Equivalence golden suite** | Differential Harbor vs TraceTensor suite not closed — see [HARBOR_PARITY_PLAN.md](../HARBOR_PARITY_PLAN.md). |
| **Tier-2 breadth** | GPU/TPU, docker-compose multi-container, MCP, Windows, multi-step tasks — open. |
| **ATIF as first-class export** | Trajectories stored; full Harbor ATIF parity / hub export still a stretch goal. |

### Explicit non-goals (for now)

- Replacing Harbor’s kernel with a different evaluation model.
- Agentic monorepo discovery as default intake.
- Alternate on-disk package names (`prompt.md` / `runtime/` / `checks/`) as product input.
- “Flow at any cost” automation that invents ground truth.

---

## 4. Bottom line

TraceTensor already implements Harbor’s **core spine**:

```text
evaluation folder → job/run → Docker → agent → tests (score) → trajectory / UI
```

Harbor’s remaining lead is mostly **breadth** (cloud sandboxes, Terminus, hub), not a different architecture.

When extending TraceTensor, map new work onto this same spine first; add Harbor breadth only when product priority demands it.

---

## 5. Related docs

| Doc | Role |
|---|---|
| [HARBOR_PARITY_PLAN.md](../HARBOR_PARITY_PLAN.md) | Phased parity roadmap |
| [ARCHITECTURE.md](../../ARCHITECTURE.md) | TraceTensor internal layering |
| [DeepWiki Harbor Overview](https://deepwiki.com/harbor-framework/harbor/1-overview) | Upstream Harbor learning source |
