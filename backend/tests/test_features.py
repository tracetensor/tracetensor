"""
Offline suite for the platform-extension features:
  Reward Kit (weighted/partial aggregation), LLM-judge parsing, the environment
  plug-in registry, CLI dataset resolution, and the public `tracetensor` API.

All pure logic — no Docker, no API key.

Run:  cd backend && python tests/test_features.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
PROJECT = HERE.parents[2]
sys.path.insert(0, str(BACKEND))

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


print("== Reward Kit — weighted / partial aggregation ==")
from app.services.verifier import _aggregate, _parse_reward  # noqa: E402

_spec = {"aggregate": "weighted_mean", "weights": {"correctness": 0.7, "style": 0.3}}
check(
    "weighted_mean",
    abs(_aggregate({"correctness": 1.0, "style": 0.5}, _spec) - 0.85) < 1e-9,
    str(_aggregate({"correctness": 1.0, "style": 0.5}, _spec)),
)
check("min", _aggregate({"a": 0.2, "b": 0.9}, {"aggregate": "min"}) == 0.2)
check("max", _aggregate({"a": 0.2, "b": 0.9}, {"aggregate": "max"}) == 0.9)
check("sum", _aggregate({"a": 0.2, "b": 0.3}, {"aggregate": "sum"}) == 0.5)
check("mean", abs(_aggregate({"a": 0.2, "b": 0.4}, {"aggregate": "mean"}) - 0.3) < 1e-9)
check(
    "weights select which metrics count (min over listed only)",
    _aggregate({"a": 0.1, "b": 0.9, "c": 0.5}, {"aggregate": "min", "weights": {"b": 1, "c": 1}})
    == 0.5,
)
# _parse_reward: canonical "reward" wins; multi-metric uses the spec if given.
r, _ = _parse_reward(None, '{"reward": 0.4, "x": 1.0}')
check("_parse_reward: canonical 'reward' key wins", r == 0.4)
r, _ = _parse_reward(None, '{"correctness": 1.0, "style": 0.5}', _spec)
check("_parse_reward: multi-metric aggregates via the Reward Kit spec", abs(r - 0.85) < 1e-9)
r, _ = _parse_reward(None, '{"correctness": 1.0, "style": 0.0}')
check("_parse_reward: multi-metric without a spec falls back to mean", abs(r - 0.5) < 1e-9)

print("\n== LLM-judge — score + model parsing ==")
from app.services.llm_judge import _parse_score, _split_model  # noqa: E402

check("score from JSON", _parse_score('{"score": 0.8, "reasoning": "ok"}') == 0.8)
check("score from JSON embedded in prose", _parse_score('Here: {"score": 0.6}') == 0.6)
check("score from a bare number", _parse_score("0.75") == 0.75)
check("score clamps above 1", _parse_score('{"score": 1.7}') == 1.0)
check("score clamps below 0", _parse_score('{"score": -3}') == 0.0)
check("unparseable score -> None", _parse_score("no number here at all") is None)
check(
    "split 'openai/gpt-4.1-mini'", _split_model("openai/gpt-4.1-mini") == ("openai", "gpt-4.1-mini")
)
check("split bare provider -> its default model", _split_model("anthropic")[0] == "anthropic")
check("split bare model -> assume openai", _split_model("gpt-4o") == ("openai", "gpt-4o"))

print("\n== Environment plug-in registry ==")
from app.services.environment import (  # noqa: E402
    BaseEnvironment,
    DockerEnvironment,
    ExecResult,
    PodmanEnvironment,
    available_backends,
    make_environment,
    register_environment,
)

check("docker + podman are registered", {"docker", "podman"} <= set(available_backends()))
check(
    "podman backend -> PodmanEnvironment",
    type(make_environment("podman", Path("/tmp"))).__name__ == "PodmanEnvironment",
)
check("podman swaps the container CLI", make_environment("podman", Path("/tmp"))._CLI == "podman")
check("docker keeps its CLI", make_environment("docker", Path("/tmp"))._CLI == "docker")
check(
    "PodmanEnvironment is a DockerEnvironment subclass",
    issubclass(PodmanEnvironment, DockerEnvironment),
)


class _Custom(BaseEnvironment):
    """A minimal third-party backend — implements the full contract, does nothing."""

    def __init__(self, task_dir: Path, **kwargs) -> None:
        self.task_dir = task_dir

    def build(self) -> None: ...
    def setup(self) -> None: ...

    def exec(self, command, phase="agent", timeout=None, as_user=None, env=None):
        return ExecResult(command=command, exit_code=0, stdout="", stderr="", duration_s=0.0)

    def read_file(self, path):
        return None

    def write_file(self, path, content) -> None: ...
    def copy_in(self, src_dir, dest) -> None: ...
    def set_network(self, mode) -> None: ...

    def transfer_from(self, other, paths):
        return []

    def teardown(self) -> None: ...


register_environment("custom-x", _Custom)
check(
    "a custom backend registers + resolves",
    type(make_environment("custom-x", Path("/tmp"))) is _Custom,
)


# A backend that forgets part of the contract must fail at construction, not
# return None from exec() and silently score every trial 0.0.
class _Incomplete(BaseEnvironment):
    def __init__(self, task_dir: Path, **kwargs) -> None:
        self.task_dir = task_dir


try:
    _Incomplete(Path("/tmp"))
    check("an incomplete backend can't be instantiated", False)
except TypeError as _exc:
    check("an incomplete backend can't be instantiated", "exec" in str(_exc), str(_exc)[:80])
try:
    make_environment("nope", Path("/tmp"))
    check("unknown backend raises", False)
except ValueError:
    check("unknown backend raises", True)
try:
    register_environment("bad", str)  # not a BaseEnvironment
    check("registering a non-BaseEnvironment raises", False)
except TypeError:
    check("registering a non-BaseEnvironment raises", True)

print("\n== CLI dataset resolution ==")
from app.cli.dataset import _task_dirs  # noqa: E402

_ex = PROJECT / "examples"
check(
    "_task_dirs finds task dirs in a dataset directory",
    len(_task_dirs(_ex)) >= 2,
    str(len(_task_dirs(_ex))),
)
check(
    "_task_dirs on a single task returns just it", _task_dirs(_ex / "fix-add") == [_ex / "fix-add"]
)

print("\n== Public API facade ==")
import tracetensor  # noqa: E402

for name in (
    "run_trial",
    "make_agent",
    "resolve_agent",
    "make_environment",
    "register_environment",
):
    check(f"tracetensor.{name} is exported + callable", callable(getattr(tracetensor, name, None)))
check("tracetensor.__version__ is set", bool(tracetensor.__version__))

print("\n== verifier.type schema ==")
from app.schemas.task import VerifierConfig  # noqa: E402

check("type='llm-judge' is valid", VerifierConfig(type="llm-judge").type == "llm-judge")
check("type defaults to 'script'", VerifierConfig().type == "script")
try:
    VerifierConfig(type="bogus")
    check("invalid verifier.type raises", False)
except Exception:
    check("invalid verifier.type raises", True)

print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
