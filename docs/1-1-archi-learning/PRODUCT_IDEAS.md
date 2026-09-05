# TraceTensor product ideas (not Harbor gaps)

Capture product / DX ideas we have discussed or noted but **have not decided to build yet**.

**Not in this file:** Harbor parity gaps → [GAPS.md](GAPS.md).  
**Stance:** TraceTensor is local-first; Harbor-compatible evaluation folders. Ideas here may extend the product *around* that kernel.

**Last updated:** 2026-07-27

---

## Status legend

| Tag | Meaning |
|---|---|
| **Parked** | Explicitly deferred / non-goal for now |
| **Idea** | Worth discussing; no commitment |
| **Candidate** | Strong local DX candidate when prioritized |

---

## 1. Discovery control & cost (parked agentic path)

Context: former agentic workspace / monorepo discovery was costly and was removed. **Not** default intake. Keep ideas so we don’t rediscover them if discovery returns as a deliberate product.

### 1.1 Observability → control loop

**Status:** Idea / Parked with discovery  

**Idea:** We already log cost/time by phase on trials. What’s missing for a discovery product is feeding those signals into the *discovery* loop:

- Wave duration  
- Accepts / rejects  
- Empty tools  

→ decide **continue vs stop**.

**Effect:** Makes multi-wave discovery reliable and general.  
**Quality impact:** None on acceptance rules — better stop decisions only.

**Depends on:** A discovery wave runner existing again (currently absent).

### 1.2 Agentic early-stop (coverage-based, not keywords)

**Status:** Idea / Parked with discovery  

**Idea:** After each discovery wave, stop when:

- Soft targets are met, **or**  
- Accepted sources already cover the angle well (counts + accept rate / diversity)  

…instead of burning more Pro waves on empty hunts (e.g. social subagents).

**Effect:** Cuts long discovery runtime and $ spend.  
**Quality impact:** Neutral-to-positive **if** stop only when soft targets / floors are already hit — not lowering the bar; stopping pointless chase.

**Depends on:** Same discovery wave runner + accept/reject metrics from 1.1.

### 1.3 Related parked stance

| Item | Status |
|---|---|
| Agentic monorepo discovery as **default** intake | **Parked** (non-goal) |
| Inventing ground truth / oracles automatically | **Parked** (non-goal) |

---

## 2. Authoring DX (local candidates)

These improve creating Harbor-shaped evaluations without changing the on-disk contract.

| Idea | Status | Notes | Related GAPS |
|---|---|---|---|
| `tasks init` scaffold (`org/name`, templates, flags) | **Candidate** | High local value; no Hub needed | §37 |
| `task start-env` interactive Docker shell | **Candidate** | Explore env before full trial | §36 / §40 |
| QualityChecker-style LLM task lint | **Idea** | Test leaks / reward hacking / missing content | §40 |
| In-product authoring wizard | **Parked** | Deferred; Cursor kit later | §8 |
| Guided create-task docs in product | **Idea** | Mirror Harbor steps 1–8 with TT CLI names | §41 |

---

## 3. Nomenclature & product surface

| Idea | Status | Notes |
|---|---|---|
| Finish UI/API rename (evaluation / run / score) | **Candidate** | Wire leftovers: `patient_chart`, `ready_for_examination`, etc. |
| Keep Harbor file names on disk forever | **Locked** | `task.toml`, `reward.txt`, … |

---

## 4. Dataset / registry product (beyond local)

| Idea | Status | Notes | Related GAPS |
|---|---|---|---|
| Resolve `name@version` from Harbor registry | **Idea** / Left | Full catalog pipeline | §29–§35 |
| Package datasets / Hub publish | **Idea** / Left | Supabase / packages | §33 |
| Adapter framework (not one-off SWE-Bench) | **Idea** | Pluggable external → Harbor dirs | §42 |
| Harbor-style task cache (`~/.cache/…`) | **Idea** | Only with registry download | §34 |

---

## 5. Results / training / Viewer

| Idea | Status | Notes | Related GAPS |
|---|---|---|---|
| ATIF-native trajectories | **Idea** | Interop with Harbor Viewer / training | §19 |
| `traces export` → RL / ShareGPT datasets | **Idea** | Training pipeline | §19 |
| Dedicated Viewer app (vs monolith HTML) | **Idea** | Product polish | §19 |

---

## 6. Execution breadth (when product demands)

| Idea | Status | Notes |
|---|---|---|
| First cloud env (Modal / Daytona) | **Idea** | Scale, not local parity |
| Terminus-2 built-in agent | **Idea** / Deferred | Separate track |
| Multi-step tasks | **Idea** | Local-capable Harbor feature |
| Compose / GPU / MCP / Windows | **Idea** / Left | Env breadth |

---

## 7. How to use this file

1. New product thoughts that are **not** “match Harbor X” → add a row or subsection here.  
2. When something becomes a committed roadmap item, move it to a real plan (or implement) and mark **Done** or delete.  
3. Do **not** mix into [GAPS.md](GAPS.md) unless it is true Harbor parity debt.

---

## Related

| Doc | Role |
|---|---|
| [GAPS.md](GAPS.md) | Harbor vs TT gaps (local / Left) |
| [HARBOR_TT_MAPPING.md](HARBOR_TT_MAPPING.md) | Arch mapping + nomenclature |
| [HARBOR_PARITY_PLAN.md](../HARBOR_PARITY_PLAN.md) | Phased parity roadmap |
