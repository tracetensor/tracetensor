# TraceTensor OSS roadmap

How we ship TraceTensor as open source without overclaiming.

| Doc | Role |
|---|---|
| [LAUNCH_PLAN.md](LAUNCH_PLAN.md) | **Ship checklist** — exit criteria for an honest public launch |
| [1-1-archi-learning/GAPS.md](1-1-archi-learning/GAPS.md) | Harbor surface **catalog** (not a must-clear list) |
| [HARBOR_PARITY_PLAN.md](HARBOR_PARITY_PLAN.md) | Tier-1 equivalence gate (optional claim) |
| This file | **Product phasing** — Phase 1 vs Phase 2 |

## Positioning

TraceTensor is a **self-hosted** framework for testing and improving AI agents in
**isolated sandboxes** (local Docker in Phase 1). Define evaluations, run agents,
score results, browse trajectories, and compare runs in a local **Vault**.

We do **not** claim Harbor cloud/Hub/Terminus parity at launch. Language stays:
*credible self-hosted Harbor-compatible eval harness* until
[LAUNCH_PLAN.md](LAUNCH_PLAN.md) Phase 3 (differential golden suite) passes.

## Phase 1 (current product)

Local-only prove/improve loop:

- Harbor-compatible evaluations (`task.toml`, `instruction.md`, `environment/`, `tests/`)
- Docker sandboxes; agent install/run; verifier scoring; trajectories
- CLI + web dashboard; dataset leaderboard
- **Vault (local):** browse / show / export runs (`tracetensor vault …` + dashboard Vault)
- **Thin usage:** duration + tokens (+ estimate `$` when priced) for compare — not a billing lab
- Launch docs + [SECURITY.md](../SECURITY.md); honest agent status labels in code + README

**Not in Phase 1:** Vault remote share, cloud sandboxes, prompt A/B lab, full `$`
cost engine, RL training-dataset packs, docs-as-Hub inside Vault.

## Phase 2 (complete the thin / deferred vision)

| Phase 1 (thin) | Phase 2 (complete) |
|---|---|
| Local Vault browse/export | Vault **share / push** |
| Launch README docs | Docs as Vault pillar (Hub-like) |
| Tokens + duration | Full usage + **$ cost × speed** view |
| Manual run-vs-run | **Prompt A/B** workflow |
| Raw trajectory export | Structured **RL / training** packs |
| Local Docker | **Cloud** sandboxes |
| Local multi-trial | Eval / RL **at scale** + shared compares |

## Related

- [SCALING.md](SCALING.md) — scale via workers + Docker hosts (not cloud-first)
- [OPERATIONS.md](OPERATIONS.md) — migrations, backups, drain
- [1-1-archi-learning/HARBOR_TT_MAPPING.md](1-1-archi-learning/HARBOR_TT_MAPPING.md) — Harbor → TT map
