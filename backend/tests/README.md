# Test tiers

| Tier | When | What |
|------|------|------|
| **T0 offline** | Always (`pytest -m "not docker"`) | Registry mocks, platform, preflight, infra retry |
| **T1 Docker** | CI when Docker available | Oracle examples, phase1 fidelity |
| **T2 Hub stress** | Manual (`TRACETENSOR_HUB_STRESS=1 scripts/hub_stress.sh`) | Live Harbor Hub pull + optional oracle |

Run offline CI:

```bash
cd backend && venv/bin/python tests/run_ci.py --offline-only
```

Run full CI (includes Docker tier when daemon is up):

```bash
cd backend && venv/bin/python tests/run_ci.py
```
