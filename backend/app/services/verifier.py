"""
Verifier — the health check.

Doctor analogy: after the patient (agent) has worked, the verifier runs the
health check (tests/test.sh) inside the same room and reads the result.

Convention: test.sh writes a reward to
  /logs/verifier/reward.txt   -> a single number (e.g. "1" or "0.5"), OR
  /logs/verifier/reward.json  -> a JSON dict of named criteria.

We support both. For reward.json we average the numeric values to a scalar
reward in [0, 1] and keep the full dict as the payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.services.environment import BaseEnvironment, ExecResult


@dataclass
class VerifierResult:
    reward: float
    passed: bool
    payload: dict | None
    log: str
    step: ExecResult


# ----------------------------------------------------------------------
# Reward Kit — aggregate a multi-metric reward.json into one scalar.
# ----------------------------------------------------------------------
# A task can drop a `tests/reward.toml` describing how its named criteria roll
# up, instead of the default (mean). This is TraceTensor's Reward Kit: partial
# credit + weighted / min / max / sum aggregation, authored per task. Example:
#
#   aggregate = "weighted_mean"   # weighted_mean | mean | min | max | sum
#   [weights]
#   correctness = 0.7
#   style       = 0.3
#
# With reward.json = {"correctness": 1.0, "style": 0.5} → 0.7*1 + 0.3*0.5 = 0.85.
_AGGREGATORS = ("weighted_mean", "mean", "min", "max", "sum")


def _load_reward_spec(tests_dir: Path) -> dict | None:
    """Read `tests/reward.toml` (the Reward Kit spec) if present."""
    path = tests_dir / "reward.toml"
    if not path.exists():
        return None
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:  # pragma: no cover - 3.9/3.10 path
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        parsed: dict = tomllib.loads(path.read_text())
        return parsed
    except Exception:
        return None


def _aggregate(data: dict, spec: dict) -> float:
    """Roll a named-criteria dict up per the Reward Kit spec."""
    nums = {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
    if not nums:
        return 0.0
    agg = str(spec.get("aggregate", "weighted_mean")).lower()
    weights = {k: float(v) for k, v in (spec.get("weights") or {}).items() if k in nums}
    if agg == "weighted_mean" or (agg not in _AGGREGATORS and weights):
        pairs = list(weights.items()) or [(k, 1.0) for k in nums]
        total_w = sum(w for _, w in pairs)
        return sum(nums[k] * w for k, w in pairs) / total_w if total_w else 0.0
    # For non-weighted aggregators, restrict to the listed metrics if weights were
    # given (they select which criteria count), else use them all.
    vals = [nums[k] for k in weights] if weights else list(nums.values())
    if agg == "min":
        return min(vals)
    if agg == "max":
        return max(vals)
    if agg == "sum":
        return sum(vals)
    return sum(vals) / len(vals)  # mean (and safe default)


def _parse_reward(
    txt: str | None, js: str | None, spec: dict | None = None
) -> tuple[float, dict | None]:
    """Return (scalar_reward, payload_dict_or_None) — standard semantics + Reward Kit.

    The designated main score is the key **"reward"** in reward.json
    ("set the name of an aggregated score to `reward` to make it the main score").
    So:
      1. reward.json dict with a numeric "reward" key -> use it (canonical).
      2. reward.json that is a single number, or a dict with exactly one numeric
         value -> use that number.
      3. otherwise (multi-metric dict without "reward") -> aggregate via the Reward
         Kit spec (tests/reward.toml) if present; else fall back to the mean.
      4. reward.txt -> a single number.
    """
    if js:
        try:
            data = json.loads(js)
            if isinstance(data, (int, float)):
                return float(data), {"reward": float(data)}
            if isinstance(data, dict) and data:
                if isinstance(data.get("reward"), (int, float)):  # canonical
                    return float(data["reward"]), data
                # Common single-score aliases (compat with non-canonical tasks
                # that name their one score differently). Prefer "reward".
                for k in ("score", "sorted_correctly", "report_correct", "correct", "accuracy"):
                    if isinstance(data.get(k), (int, float)):
                        return float(data[k]), data
                nums = [float(v) for v in data.values() if isinstance(v, (int, float))]
                if len(nums) == 1:
                    return nums[0], data
                # Multi-metric without a recognized score: aggregate via the Reward
                # Kit spec if the task provides one, else mean (so tasks still score).
                if spec:
                    return _aggregate(data, spec), data
                return (sum(nums) / len(nums) if nums else 0.0), data
        except (ValueError, json.JSONDecodeError):
            pass
    if txt:
        try:
            return float(txt.strip()), None
        except ValueError:
            pass
    return 0.0, None


def run_verifier(
    env: BaseEnvironment,
    tests_dir: Path,
    timeout: float | None = None,
    pass_threshold: float = 1.0,
    verifier_user: str = "0",
    copy_tests: bool = True,
) -> VerifierResult:
    """Run test.sh as the verifier user and read the reward.

    In shared mode we copy tests/ into the agent's container. In separate mode
    the tests are already baked into the verifier image (copy_tests=False).
    The verifier runs as root by default so it can write the root-owned,
    agent-unwritable /logs/verifier reward dir.
    """
    if copy_tests:
        env.copy_in(tests_dir, "/tests")

    # Ensure the reward dir exists (root), then run the health check as verifier.
    env.exec("mkdir -p /logs/verifier", phase="verifier", as_user="0")
    step = env.exec("bash /tests/test.sh", phase="verifier", timeout=timeout, as_user=verifier_user)

    reward_txt = env.read_file("/logs/verifier/reward.txt")
    reward_json = env.read_file("/logs/verifier/reward.json")
    reward, payload = _parse_reward(reward_txt, reward_json, _load_reward_spec(tests_dir))

    log_parts = []
    if step.stdout:
        log_parts.append("[stdout]\n" + step.stdout[-4000:])
    if step.stderr:
        log_parts.append("[stderr]\n" + step.stderr[-2000:])
    if reward_txt:
        log_parts.append("[reward.txt]\n" + reward_txt.strip())
    if reward_json:
        log_parts.append("[reward.json]\n" + reward_json.strip())

    return VerifierResult(
        reward=reward,
        passed=reward >= pass_threshold,
        payload=payload,
        log="\n\n".join(log_parts),
        step=step,
    )
