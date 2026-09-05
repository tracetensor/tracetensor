# Scoring: `reward.txt` / `reward.json` / TT `reward.toml`

TraceTensor scores a trial by reading the verifier’s output under
`/logs/verifier/`:

| File | Meaning |
|---|---|
| `reward.txt` | Single float (or int) score, usually `0` or `1` |
| `reward.json` | `{"reward": 0.8}` or a map of named criteria |
| `tests/reward.toml` | **TraceTensor Reward Kit** — how to aggregate named criteria |

## TraceTensor `tests/reward.toml` (not Harbor’s pip package)

Harbor documents a separate **harbor-rewardkit** / `uvx` workflow for some tasks.
TraceTensor’s `tests/reward.toml` is our **own** small aggregator (see
`examples/reward-kit-demo/`): it tells the harness how to combine metrics already
written into `reward.json` (e.g. `weighted_mean`, `min`, `max`).

It is **not** a drop-in for installing Harbor’s rewardkit package into the
sandbox. If you are porting a Harbor task that shells out to `harbor-rewardkit`
/ `uvx`, either:

1. Keep writing a final scalar to `reward.txt` / `reward.json` from `test.sh`, or
2. Adapt criteria into TT’s `tests/reward.toml` + JSON metrics (see the demo).

## CTRF

Common Test Report Format (`ctrf.json`) is **not** read. `tasks validate` warns
if it finds CTRF files under `tests/`. Prefer `reward.txt` / `reward.json`.

## MCP / Computer-1

If `task.toml` references MCP servers or a Computer-1 / GUI environment type,
`tasks validate` emits a warning — those Harbor surfaces are unsupported in
Phase 1 (see [OSS_ROADMAP.md](OSS_ROADMAP.md)).
