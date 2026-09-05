# Pricing and cost estimates (deferred)

**Status:** Cost tracking is **off** for Phase 1 OSS launch.

TraceTensor shows **tokens** and **latency** on every run. **Dollar amounts are not shown** until we ship a honest estimate layer.

---

## Why deferred

- Model prices change often (GPT 5.4, 5.6, new Claude ids, etc.).
- A small hardcoded table in code always lags → blank `$` or wrong trust.
- Users do **not** need OpenRouter keys; a future solution will ship a **bundled price file** updated at release time.

---

## What works today

| Metric | Shown? |
|--------|--------|
| Input / output tokens | Yes |
| Latency | Yes (logs + trajectory) |
| Pass / fail / reward | Yes |
| USD cost | **No** (disabled in code) |
| `dataset run --budget` | **No** (disabled while cost is off) |

---

## Planned approach (when re-enabled)

1. Ship `model_prices.json` (maintainer-updated from public catalogs).
2. Lookup by model id (`gpt-5.4-mini`, `openai/gpt-5.4-mini`, …).
3. Label: **estimated** | **from_agent** | **unknown** — never fake a number.
4. Optional user override: `pricing.yaml` (gitignored).

See product discussion in repo history; OpenRouter `/api/v1/models` works as a **maintainer data source** (no user key required for catalog refresh).

---

## Code toggle

```python
# backend/app/services/cost.py
COST_TRACKING_ENABLED = False  # flip when pricing ships
```

When `False`:

- `estimate_cost_usd()` always returns `None`
- Trial / Vault rollups omit `cost_usd`
- CLI, Vault, and dashboard hide `$` columns

---

## Related docs

- [BUG_HUNT.md](BUG_HUNT.md) — run quality loop (tokens are enough for compare today)
- [OSS_ROADMAP.md](OSS_ROADMAP.md) — Phase 2 may include cost compare
