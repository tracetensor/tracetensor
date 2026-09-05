# TraceTensor gaps vs Harbor

Canonical gap list. Combines:

- Gaps from [HARBOR_TT_MAPPING.md](HARBOR_TT_MAPPING.md)
- Gaps from Harbor DeepWiki overview (subsystems, execution flow, environments, agents, outputs)
- Gaps still open in [HARBOR_PARITY_PLAN.md](../HARBOR_PARITY_PLAN.md)

**Last updated:** 2026-07-26

---

## Status legend

| Tag | Meaning |
|---|---|
| **Missing** | Not implemented |
| **Partial** | Exists but not Harbor-equivalent |
| **Gated** | Code exists; blocked on keys / image / infra |
| **Deferred** | Deliberate non-goal for now |

---

## 1. Equivalence & credibility

| Gap | Status | Notes |
|---|---|---|
| **Differential golden suite** | Missing | Same Harbor tasks × agent × N runs on Harbor vs TraceTensor; assert reward/pass match. Blocks “Tier‑1 equivalent” answer. |
| **Full reproducibility bundle** | Partial | Trajectories/results saved; image digest / workspace diff / seed not all downloadable as a Harbor-style bundle. |
| **Infra vs task failure classification** | Partial | Queue reclaims dead workers; typed retry taxonomy still incomplete vs Harbor job/trial semantics. |

---

## 2. Execution (Jobs & Trials)

| Gap | Status | Notes |
|---|---|---|
| **Harbor `Job` / `TrialQueue` shape** | Partial | We have Job + Postgres queue + `run_trial`. Not the same classes/APIs as `harbor.job.Job` / `TrialQueue`. |
| **`JobConfig` parity** | Partial | CLI/API flags cover core path; not every Harbor job config knob. |
| **Trial lifecycle hooks** | Partial | setup → agent → verify → score → teardown works; Harbor’s finer hook/extension surface not mirrored. |
| **Multi-step tasks (`[steps]`)** | Missing | Harbor multi-step + `multi_step_reward_strategy` (mean/final) not first-class. |
| **Parallel cloud fan-out** | Missing | Local/parallel Docker only; Harbor scales trials across cloud sandboxes. |

---

## 3. Environments

| Gap | Status | Notes |
|---|---|---|
| **Docker** | Done | Primary backend. |
| **Podman** | Done | Second backend via factory. |
| **LocalEnvironment** | Missing | Mentioned in comments; not registered. |
| **Daytona** | Missing | Cloud sandbox |
| **Modal** | Missing | Serverless / GPU |
| **E2B** | Missing | Cloud sandbox |
| **Apple Container** | Missing | Apple Silicon VMs |
| **Novita** | Missing | AI sandbox platform |
| **Islo** | Missing | MicroVM |
| **TensorLake** | Missing | Cloud + resources |
| **CWSandbox** | Missing | CoreWeave |
| **W&B Sandboxes** | Missing | Weights & Biases |
| **Use-Computer** | Missing | GUI / remote desktop |
| **Blaxel** | Missing | Dynamic network policies |
| **OpenSandbox** | Missing | OSS cloud sandbox |
| **Beam** | Missing | Serverless containers |
| **GKE / Runloop / others** | Missing | From Harbor factory list |
| **`allowlist` network** | Missing | Correctly **rejected** today; needs nftables egress sidecar to implement. |
| **GPU / TPU** | Missing | `gpus` / `gpu_types` / `[environment.tpu]` |
| **Multi-container / Compose** | Missing | `docker-compose.yaml` + sidecar collect |
| **MCP servers / healthcheck** | Missing | `[environment].mcp_servers`, healthcheck |
| **Windows containers** | Missing | `os = windows`, `test.bat` / `solve.bat` |

---

## 4. Agents

| Gap | Status | Notes |
|---|---|---|
| **OracleAgent** | Done | Runs `solution/solve.sh` (author self-test; not our default validation preference). |
| **Installed: mini-swe, claude-code, codex** | Done | Verified on real API runs. |
| **Installed: gemini** | Gated | Needs `GEMINI_API_KEY`. |
| **Installed: copilot** | Gated | Needs Copilot-entitled GitHub token. |
| **Installed: openhands** | Gated | Needs purpose-built image (non-root UID + Playwright). |
| **Terminus 2** | Deferred / Missing | Harbor’s built-in LLM terminal agent + native ATIF; not an installed-agent adapter. Separate track. |
| **Harbor AgentFactory / BaseAgent surface** | Partial | We have `make_agent` / `BaseInstalledAgent`; not full Harbor plugin ecosystem (Computer-1, DspyRlm, etc.). |
| **MCP agent integration** | Missing | Harbor MCP server integration path. |

---

## 5. Tasks & Datasets / Registry

| Gap | Status | Notes |
|---|---|---|
| **Harbor task folder format** | Done | Exact: `task.toml`, `instruction.md`, `environment/`, `tests/`, `solution/`. |
| **Local ingest + validate** | Done | Upload / CLI validate. |
| **Dataset runs + leaderboard** | Done | API/UI + `tracetensor dataset run`. |
| **Dataset pull (git / swebench)** | Done | Local registry-style pull. |
| **Harbor Hub client** | Partial | `tracetensor tasks pull` + `dataset pull org/name@tag` via direct registry API; no publish/auth yet. |
| **TaskClient / RegistryClient** | Partial | [`harbor_registry.py`](../backend/app/services/harbor_registry.py) + cache; not full Harbor SDK parity. |
| **Custom metrics / hub metrics** | Missing | Harbor hub custom metrics. |
| **HF dataset adapters at Harbor scale** | Partial | SWE-Bench importer exists; not full Harbor adapter zoo. |

---

## 6. Verification & scoring

| Gap | Status | Notes |
|---|---|---|
| **`test.sh` → reward.txt/json** | Done | |
| **Isolated verifier** | Done | Tamper-resistant separate container. |
| **Reward Kit (`reward.toml`)** | Done | Weighted / agg rules. |
| **LLM-judge verifier** | Done | |
| **Harbor Verifier class / all modes** | Partial | Core path solid; Harbor’s `dev` / `mock` / `resource` verifier modes not all mirrored as named modes. |
| **Multi-step reward strategies** | Missing | mean / final across steps. |

---

## 7. Trajectory & outputs

| Gap | Status | Notes |
|---|---|---|
| **Per-run result + trajectory storage** | Done | DB + `runs/` artifacts. |
| **ATIF v1.7 as first-class format** | Partial | We store trajectories; not claiming Harbor ATIF parity or ATIF tooling. |
| **`trajectory.json` export identical to Harbor Viewer** | Partial | Our dashboard reads our shape; not Harbor Viewer drop-in. |
| **Trace export → training datasets** | Missing | Harbor “trace export & dataset generation” pipeline. |
| **Harbor Viewer app** | Missing | We have a single-file dashboard, not `apps/viewer`. |

---

## 8. Viewer / product DX

| Gap | Status | Notes |
|---|---|---|
| **Web results UI** | Done | Register, run, live SSE, leaderboard. |
| **Harbor Viewer feature parity** | Partial | Missing Harbor’s job/trial deep routes, analyze-models, etc. |
| **Nomenclature pass** | Partial | evaluation / run / score in places; API field names (`patient_chart`, `trial`, status `ready_for_examination`) still Harbor/doctor-era in places. |
| **Authoring DX (Cursor kit / wizard)** | Deferred | Package contract only for now; no in-product wizard; no agentic repo discovery. |

---

## 9. From original mapping doc (carried forward)

These were listed in `HARBOR_TT_MAPPING.md` §3 and remain authoritative:

1. Terminus-2  
2. Cloud environments (full list expanded in §3 above)  
3. Harbor Hub  
4. `allowlist` network  
5. LocalEnvironment  
6. Equivalence golden suite  
7. Tier-2 breadth (GPU/TPU, compose, MCP, Windows, multi-step)  
8. ATIF as first-class export  

---

## 10. New gaps found from DeepWiki deep-dive

Added beyond the first mapping pass:

| New gap | Why it matters |
|---|---|
| **Named cloud env matrix** | Harbor documents 12+ concrete backends; we only had “cloud missing” as one line. |
| **JobConfig / lifecycle hooks** | Execution flow docs show richer job setup than our CLI/API surface. |
| **Verifier mode taxonomy** | Harbor verifier modes beyond script/llm-judge/separate. |
| **Trace → dataset generation** | Harbor positions trajectories as RL/training assets end-to-end. |
| **Viewer app parity** | Separate FastAPI+React viewer vs our monolith HTML. |
| **Agent ecosystem beyond installed six** | Computer-1, DspyRlm, MCP agents, etc. |
| **API/schema nomenclature debt** | Product words updated; wire formats still mixed. |
| **Installed-agent gates** | gemini / copilot / openhands not fully production-ready. |

---

## 11. Explicit non-goals (not gaps to “fix”)

- Replacing Harbor’s evaluation model with a different kernel.  
- Agentic monorepo discovery as default intake.  
- Alternate on-disk names (`prompt.md` / `runtime/` / `checks/`) as the input contract.  
- Inventing ground truth / oracles automatically.  
- Matching every Harbor cloud provider before Tier‑1 equivalence is proven.

---

## 12. Priority hint (not a roadmap commitment)

1. **Equivalence golden suite** — credibility  
2. **Close agent gates** (gemini/copilot/openhands) if product needs them  
3. **ATIF / export polish** — if training-data story matters  
4. **Cloud env #1** (e.g. Modal or Daytona) — only when scale demands  
5. **Terminus-2** — separate track  
6. **Hub** — when sharing/publishing matters  

---

## Related

| Doc | Role |
|---|---|
| [HARBOR_TT_MAPPING.md](HARBOR_TT_MAPPING.md) | Arch learning + 1:1 map |
| [HARBOR_PARITY_PLAN.md](../HARBOR_PARITY_PLAN.md) | Phased parity + Equivalence Gate |
| [DeepWiki Harbor Overview](https://deepwiki.com/harbor-framework/harbor/1-overview) | Upstream source |
