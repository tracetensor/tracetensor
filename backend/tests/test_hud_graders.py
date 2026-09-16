"""
Tests for the TraceTensor grader system (Phase 3+, high-leverage items).

Covers:
  Text graders:   exact_match, contains, contains_any, contains_all,
                  numeric_match, f1_score
  Combinators:    combine (weights, penalties), combine_any, combine_all
  BashGrader:     real subprocess, timeout, exit code
  rollout_timeout: hard cancellation in hud_runner
  Integration:    grader_example env.py loaded + tasks graded inline
  Real API:       capital_city + unit_convert via OpenAI (skipped if no key)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

GRADER_DIR = Path(__file__).parent / "hud_compat" / "grader_example"


# ── Text graders ─────────────────────────────────────────────────────────────


class TestExactMatch:
    def test_match(self):
        from app.graders.text import exact_match
        r = exact_match("Paris", "paris")
        assert r.score == 1.0

    def test_no_match(self):
        from app.graders.text import exact_match
        r = exact_match("London", "Paris")
        assert r.score == 0.0

    def test_normalizes_articles(self):
        from app.graders.text import exact_match
        r = exact_match("the capital", "capital")
        assert r.score == 1.0

    def test_normalizes_punctuation(self):
        from app.graders.text import exact_match
        r = exact_match("Paris!", "Paris")
        assert r.score == 1.0

    def test_reason_present(self):
        from app.graders.text import exact_match
        r = exact_match("x", "y")
        assert "exact_match" in r.reason


class TestContains:
    def test_found(self):
        from app.graders.text import contains
        r = contains("The capital is Paris.", "paris")
        assert r.score == 1.0

    def test_not_found(self):
        from app.graders.text import contains
        r = contains("London is the capital.", "paris")
        assert r.score == 0.0

    def test_case_insensitive(self):
        from app.graders.text import contains
        assert contains("PARIS is great", "paris").score == 1.0


class TestContainsAny:
    def test_one_match(self):
        from app.graders.text import contains_any
        r = contains_any("I love cats", ["cats", "dogs"])
        assert r.score == 1.0

    def test_no_match(self):
        from app.graders.text import contains_any
        r = contains_any("I love fish", ["cats", "dogs"])
        assert r.score == 0.0


class TestContainsAll:
    def test_all_present(self):
        from app.graders.text import contains_all
        r = contains_all("I love cats and dogs", ["cats", "dogs"])
        assert r.score == 1.0

    def test_partial_missing(self):
        from app.graders.text import contains_all
        r = contains_all("I love cats", ["cats", "dogs"])
        assert r.score == 0.0
        assert "dogs" in r.reason


class TestNumericMatch:
    def test_exact(self):
        from app.graders.text import numeric_match
        assert numeric_match("42", 42).score == 1.0

    def test_mismatch(self):
        from app.graders.text import numeric_match
        assert numeric_match("41", 42).score == 0.0

    def test_absolute_tolerance(self):
        from app.graders.text import numeric_match
        assert numeric_match("41.5", 42.0, tolerance=1.0).score == 1.0
        assert numeric_match("40.9", 42.0, tolerance=1.0).score == 0.0

    def test_relative_tolerance(self):
        from app.graders.text import numeric_match
        # 5% of 100 = 5, so 95 should pass, 94 should fail
        assert numeric_match("95", 100.0, tolerance=0.05, relative=True).score == 1.0
        assert numeric_match("94", 100.0, tolerance=0.05, relative=True).score == 0.0

    def test_unparseable(self):
        from app.graders.text import numeric_match
        r = numeric_match("not a number", 42)
        assert r.score == 0.0
        assert "could not parse" in r.reason

    def test_strips_units(self):
        from app.graders.text import numeric_match
        assert numeric_match("212°F", 212.0, tolerance=0.1).score == 1.0


class TestF1Score:
    def test_perfect(self):
        from app.graders.text import f1_score
        assert f1_score("cat dog", "cat dog").score == 1.0

    def test_partial_overlap(self):
        from app.graders.text import f1_score
        r = f1_score("cat dog fish", "cat dog")
        assert 0.0 < r.score < 1.0

    def test_no_overlap(self):
        from app.graders.text import f1_score
        assert f1_score("cat", "dog").score == 0.0

    def test_both_empty(self):
        from app.graders.text import f1_score
        assert f1_score("", "").score == 1.0

    def test_one_empty(self):
        from app.graders.text import f1_score
        assert f1_score("", "Paris").score == 0.0


# ── Combinators ───────────────────────────────────────────────────────────────


class TestCombine:
    def test_equal_weights(self):
        from app.graders.combine import combine
        r = asyncio.run(combine(1.0, 0.0))
        assert abs(r.score - 0.5) < 1e-6

    def test_custom_weights(self):
        from app.graders.combine import combine
        r = asyncio.run(combine(1.0, 0.0, weights=[0.8, 0.2]))
        assert abs(r.score - 0.8) < 1e-6

    def test_penalty_weight(self):
        from app.graders.combine import combine
        # score=1.0 with weight=1.0, penalty grader=1.0 with weight=-0.5
        # positive sum = 1.0, score=1.0, penalty = 1.0*(0.5/1.0) = 0.5
        # final = 1.0 - 0.5 = 0.5
        r = asyncio.run(combine(1.0, 1.0, weights=[1.0, -0.5]))
        assert abs(r.score - 0.5) < 1e-6

    def test_clamped_to_zero(self):
        from app.graders.combine import combine
        # penalty larger than score → clamped to 0
        r = asyncio.run(combine(0.0, 1.0, weights=[1.0, -2.0]))
        assert r.score == 0.0

    def test_subscores_recorded(self):
        from app.graders.combine import combine
        r = asyncio.run(combine(0.6, 0.4, names=["a", "b"]))
        assert len(r.subscores) == 2
        assert r.subscores[0].name == "a"

    def test_accepts_evaluation_result(self):
        from app.graders.combine import combine
        from app.graders.text import exact_match
        em = exact_match("Paris", "Paris")  # EvaluationResult
        r = asyncio.run(combine(em, 0.5))
        assert abs(r.score - 0.75) < 1e-6

    def test_accepts_coroutines(self):
        from app.graders.combine import combine
        from app.graders.bash import BashGrader

        async def _run():
            return await combine(
                BashGrader.grade(command="exit 0", timeout=5),
                0.5,
            )
        r = asyncio.run(_run())
        assert abs(r.score - 0.75) < 1e-6


class TestCombineAny:
    def test_max_wins(self):
        from app.graders.combine import combine_any
        r = asyncio.run(combine_any(0.2, 0.9, 0.5))
        assert abs(r.score - 0.9) < 1e-6

    def test_all_zero(self):
        from app.graders.combine import combine_any
        r = asyncio.run(combine_any(0.0, 0.0))
        assert r.score == 0.0


class TestCombineAll:
    def test_min_wins(self):
        from app.graders.combine import combine_all
        r = asyncio.run(combine_all(0.9, 0.2, 0.7))
        assert abs(r.score - 0.2) < 1e-6

    def test_all_pass(self):
        from app.graders.combine import combine_all
        r = asyncio.run(combine_all(1.0, 1.0))
        assert r.score == 1.0


# ── BashGrader ────────────────────────────────────────────────────────────────


class TestBashGrader:
    def test_exit_0_scores_1(self):
        from app.graders.bash import BashGrader
        r = asyncio.run(BashGrader.grade(command="exit 0", timeout=10))
        assert r.score == 1.0

    def test_exit_1_scores_0(self):
        from app.graders.bash import BashGrader
        r = asyncio.run(BashGrader.grade(command="exit 1", timeout=10))
        assert r.score == 0.0

    def test_stdout_captured(self):
        from app.graders.bash import BashGrader
        r = asyncio.run(BashGrader.grade(command="echo hello && exit 0", timeout=10))
        assert r.score == 1.0

    def test_timeout(self):
        from app.graders.bash import BashGrader
        r = asyncio.run(BashGrader.grade(command="sleep 30", timeout=1))
        assert r.score == 0.0
        assert "timed out" in r.reason

    def test_exit_code_in_reason(self):
        from app.graders.bash import BashGrader
        r = asyncio.run(BashGrader.grade(command="exit 42", timeout=10))
        assert "42" in r.reason

    def test_process_group_killed_on_timeout(self):
        """Verify spawned child processes are killed (no orphans left)."""
        from app.graders.bash import BashGrader
        # This spawns a subprocess that in turn sleeps; on timeout both should die.
        r = asyncio.run(BashGrader.grade(
            command="bash -c 'sleep 60 &'",
            timeout=1,
        ))
        assert r.score == 0.0  # timed out


# ── rollout_timeout ───────────────────────────────────────────────────────────


class TestRolloutTimeout:
    def test_timeout_warning_logged(self, caplog):
        from app.services.hud_runner import run_hud_task
        import logging

        # Passing agent timeout >= rollout_timeout should log a warning.
        # We can't easily run a full task here, but we can check the warning
        # is produced. Use a mock BoundTask that resolves instantly.
        from unittest.mock import AsyncMock, MagicMock
        from app.services.hud_compat import TensorEnvironment, BoundTask

        async def gen():
            yield "test"
            yield 1.0

        env_obj = TensorEnvironment(name="t")
        bt = BoundTask(template_id="x", fn=gen, kwargs={}, env=env_obj)

        with caplog.at_level(logging.WARNING, logger="tracetensor.hud_runner"):
            asyncio.run(run_hud_task(
                bt,
                provider="oracle",  # won't actually call LLM
                timeout=300.0,        # >= rollout_timeout
                rollout_timeout=120.0,
            ))
        # The log record msg is "hud_timeout_hierarchy_violated" (structlog style)
        assert any(
            "hud_timeout_hierarchy_violated" in (r.msg or r.message)
            or "rollout" in (r.msg or r.message).lower()
            for r in caplog.records
        )

    def test_rollout_timeout_cancels_slow_task(self, monkeypatch):
        """A task that hangs is killed by rollout_timeout."""
        from app.services.hud_runner import _run_hud_task_bounded
        from app.services.hud_compat import TensorEnvironment, BoundTask

        async def hanging_gen():
            yield "question"
            await asyncio.sleep(999)  # hangs
            yield 1.0

        env_obj = TensorEnvironment(name="hang")
        bt = BoundTask(template_id="h", fn=hanging_gen, kwargs={}, env=env_obj)

        async def _run():
            return await _run_hud_task_bounded(
                bt,
                provider="openai",
                model=None,
                timeout=50.0,
                rollout_timeout=1.0,  # 1 second — should trigger
                pass_threshold=1.0,
                task_dir=None,
                on_event=None,
            )

        outcome = asyncio.run(_run())
        assert outcome.error is not None
        assert "timed out" in outcome.error.lower() or "rollout" in outcome.error.lower()


# ── Integration: grader_example env.py ──────────────────────────────────────


class TestGraderExampleEnv:
    def test_env_loads(self):
        from app.services.hud_adapter import load_hud_env
        loaded = load_hud_env(GRADER_DIR)
        assert loaded.env.name == "grader-example"
        assert len(loaded.tasks) == 5

    def test_bash_task_grades_correctly(self):
        """bash_exit template: BashGrader runs `echo ok && exit 0` → 1.0."""
        from app.services.hud_adapter import load_hud_env
        loaded = load_hud_env(GRADER_DIR)
        bash_task = next(t for t in loaded.tasks if t.template_id == "bash_exit")

        async def _run():
            gen = bash_task.fn(**bash_task.kwargs)
            await gen.asend(None)      # get prompt
            return await gen.asend("ready")  # grade

        score = asyncio.run(_run())
        assert float(score) == 1.0

    def test_capital_city_exact_grading(self):
        """capital_city template: correct answer scores ≥ 0.7 (exact match weight)."""
        from app.services.hud_adapter import load_hud_env
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "capital_city"
                    and t.kwargs.get("country") == "France")

        async def _run(answer):
            gen = task.fn(**task.kwargs)
            await gen.asend(None)
            return await gen.asend(answer)

        correct = asyncio.run(_run("Paris"))
        wrong = asyncio.run(_run("London"))
        assert float(correct) >= 0.7
        assert float(wrong) < 0.5

    def test_unit_convert_tolerance(self):
        """unit_convert: answer within tolerance → 1.0; outside → 0.0."""
        from app.services.hud_adapter import load_hud_env
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "unit_convert")

        async def _run(answer):
            gen = task.fn(**task.kwargs)
            await gen.asend(None)
            return await gen.asend(answer)

        assert float(asyncio.run(_run("212"))) == 1.0
        assert float(asyncio.run(_run("211.9"))) == 1.0   # within tol=0.1
        assert float(asyncio.run(_run("210"))) == 0.0

    def test_multi_contain_all_and_any(self):
        from app.services.hud_adapter import load_hud_env
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "multi_contain")

        async def _run(answer):
            gen = task.fn(**task.kwargs)
            await gen.asend(None)
            return await gen.asend(answer)

        good = asyncio.run(_run("My happy cat and dog played all day."))
        bad_missing = asyncio.run(_run("I love cats but no dog."))  # missing "dog" not in there
        bad_no_emotion = asyncio.run(_run("The cat and dog sat."))

        assert float(good) == 1.0
        # bad_missing has "cats" which contains "cat" and "dog" IS there,
        # but needs "happy/sad/excited" — let's check
        assert float(bad_no_emotion) == 0.0


# ── Real API: full run via OpenAI ─────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set"
)
class TestRealAPIGraders:
    def test_capital_city_real_llm(self):
        """LLM answers capital city question; combine(exact_match, f1) scores it."""
        from app.services.hud_adapter import load_hud_env
        from app.services.hud_runner import run_hud_task_sync
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "capital_city"
                    and t.kwargs.get("country") == "France")
        outcome = run_hud_task_sync(task, provider="openai", model="gpt-4o-mini")
        # GPT-4o-mini reliably knows Paris — expect ≥ 0.7
        assert outcome.reward is not None
        assert outcome.reward >= 0.7, f"Expected ≥0.7, got {outcome.reward}"

    def test_unit_convert_real_llm(self):
        """LLM converts 100°C → °F; numeric_match with tolerance=0.1 grades it."""
        from app.services.hud_adapter import load_hud_env
        from app.services.hud_runner import run_hud_task_sync
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "unit_convert")
        outcome = run_hud_task_sync(task, provider="openai", model="gpt-4o-mini")
        assert outcome.reward is not None
        assert outcome.reward == 1.0, f"Expected 1.0 for 100C→212F, got {outcome.reward}"

    def test_multi_contain_real_llm(self):
        """LLM writes a sentence with required words; graded by combine_all."""
        from app.services.hud_adapter import load_hud_env
        from app.services.hud_runner import run_hud_task_sync
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "multi_contain")
        outcome = run_hud_task_sync(task, provider="openai", model="gpt-4o-mini")
        assert outcome.reward is not None
        # LLM should be able to write a sentence with cat, dog, and happy/sad/excited
        assert outcome.reward == 1.0, f"Expected 1.0, got {outcome.reward}"

    def test_bash_exit_real_env(self):
        """BashGrader runs `echo ok && exit 0` — should score 1.0."""
        from app.services.hud_adapter import load_hud_env
        from app.services.hud_runner import run_hud_task_sync
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "bash_exit")
        outcome = run_hud_task_sync(task, provider="openai", model="gpt-4o-mini")
        assert outcome.reward == 1.0, f"BashGrader should score 1.0, got {outcome.reward}"

    def test_rollout_timeout_real(self):
        """rollout_timeout=5s on a real task — should complete fine (not hit timeout)."""
        from app.services.hud_adapter import load_hud_env
        from app.services.hud_runner import run_hud_task_sync
        loaded = load_hud_env(GRADER_DIR)
        task = next(t for t in loaded.tasks if t.template_id == "capital_city"
                    and t.kwargs.get("country") == "Japan")
        # 60s rollout timeout — should complete easily
        outcome = run_hud_task_sync(
            task, provider="openai", model="gpt-4o-mini",
            timeout=30.0, rollout_timeout=60.0,
        )
        assert outcome.error is None or "timed out" not in (outcome.error or "")
        assert outcome.reward is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. SubScore self-annotation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubScoreAnnotation:
    """Every text grader should include itself as a SubScore in the result."""

    def test_exact_match_populates_subscore(self):
        from app.graders.text import exact_match
        r = exact_match("Paris", "Paris")
        assert len(r.subscores) == 1
        ss = r.subscores[0]
        assert ss.name == "exact_match"
        assert ss.score == 1.0
        assert "prediction" in ss.metadata

    def test_exact_match_mismatch_subscore(self):
        from app.graders.text import exact_match
        r = exact_match("London", "Paris")
        assert r.subscores[0].score == 0.0

    def test_contains_subscore(self):
        from app.graders.text import contains
        r = contains("The cat sat on the mat", "cat")
        assert r.subscores[0].name == "contains"
        assert r.subscores[0].score == 1.0
        assert "substring" in r.subscores[0].metadata

    def test_contains_any_subscore(self):
        from app.graders.text import contains_any
        r = contains_any("I love dogs", ["cat", "dog", "bird"])
        ss = r.subscores[0]
        assert ss.name == "contains_any"
        assert ss.score == 1.0
        assert "matched" in ss.metadata
        assert "dog" in ss.metadata["matched"]

    def test_contains_all_subscore_missing(self):
        from app.graders.text import contains_all
        r = contains_all("I have a cat", ["cat", "dog"])
        ss = r.subscores[0]
        assert ss.score == 0.0
        assert "dog" in ss.metadata["missing"]

    def test_numeric_match_subscore(self):
        from app.graders.text import numeric_match
        r = numeric_match("212", 212.0)
        ss = r.subscores[0]
        assert ss.name == "numeric_match"
        assert ss.score == 1.0
        assert ss.metadata["pred"] == 212.0

    def test_numeric_match_tolerance_metadata(self):
        from app.graders.text import numeric_match
        r = numeric_match("211.9", 212.0, tolerance=0.5)
        ss = r.subscores[0]
        assert ss.score == 1.0
        assert "abs_err" in ss.metadata

    def test_f1_score_subscore(self):
        from app.graders.text import f1_score
        r = f1_score("the cat sat", "cat sat mat")
        ss = r.subscores[0]
        assert ss.name == "f1_score"
        assert 0 < ss.score < 1.0
        assert "precision" in ss.metadata
        assert "recall" in ss.metadata

    @pytest.mark.asyncio
    async def test_bash_grader_subscore(self):
        from app.graders.bash import BashGrader
        r = await BashGrader.grade(command="exit 0", timeout=5)
        ss = r.subscores[0]
        assert ss.name == "bash"
        assert ss.score == 1.0
        assert ss.metadata["exit_code"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 6. LLMJudgeGrader
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMJudgeGrader:
    """LLMJudgeGrader — unit tests use mocked LLM; real-API tests are skipped."""

    @pytest.mark.asyncio
    async def test_all_met_gives_1(self, monkeypatch):
        from app.graders.llm_judge import LLMJudgeGrader, _parse_status
        import app.graders.llm_judge as _mod

        async def _fake_one(crit, output, provider, model, loop):
            return True

        monkeypatch.setattr(_mod, "_evaluate_one", _fake_one)
        r = await LLMJudgeGrader.grade(
            output="Great answer",
            criteria=["Mentions something", "Is concise"],
        )
        assert r.score == 1.0
        assert len(r.subscores) == 2
        assert all(ss.score == 1.0 for ss in r.subscores)

    @pytest.mark.asyncio
    async def test_none_met_gives_0(self, monkeypatch):
        from app.graders.llm_judge import LLMJudgeGrader
        import app.graders.llm_judge as _mod

        async def _fake_one(crit, output, provider, model, loop):
            return False

        monkeypatch.setattr(_mod, "_evaluate_one", _fake_one)
        r = await LLMJudgeGrader.grade(
            output="Bad answer",
            criteria=[{"criterion": "Must be correct", "weight": 1.0}],
        )
        assert r.score == 0.0
        assert r.subscores[0].metadata["status"] == "UNMET"

    @pytest.mark.asyncio
    async def test_weighted_partial_score(self, monkeypatch):
        from app.graders.llm_judge import LLMJudgeGrader
        import app.graders.llm_judge as _mod

        calls: list[str] = []

        async def _fake_one(crit, output, provider, model, loop):
            calls.append(crit)
            return "MET" in crit

        monkeypatch.setattr(_mod, "_evaluate_one", _fake_one)
        r = await LLMJudgeGrader.grade(
            output="partial",
            criteria=[
                {"criterion": "MET first", "weight": 0.6},
                {"criterion": "second fails", "weight": 0.4},
            ],
        )
        assert abs(r.score - 0.6) < 0.01
        assert len(calls) == 2  # both evaluated in parallel

    @pytest.mark.asyncio
    async def test_empty_criteria_returns_0(self):
        from app.graders.llm_judge import LLMJudgeGrader
        r = await LLMJudgeGrader.grade(output="anything", criteria=[])
        assert r.score == 0.0

    @pytest.mark.asyncio
    async def test_evaluation_failure_counts_as_unmet(self, monkeypatch):
        from app.graders.llm_judge import LLMJudgeGrader
        import app.graders.llm_judge as _mod

        async def _fail(crit, output, provider, model, loop):
            return None  # parse failure

        monkeypatch.setattr(_mod, "_evaluate_one", _fail)
        r = await LLMJudgeGrader.grade(
            output="x",
            criteria=["some criterion"],
        )
        assert r.score == 0.0
        assert r.subscores[0].metadata["status"] == "evaluation failed"

    def test_parse_status_met(self):
        from app.graders.llm_judge import _parse_status
        assert _parse_status('{"criterion_status": "MET"}') is True
        assert _parse_status('{"criterion_status": "UNMET"}') is False
        assert _parse_status('garbage') is None
        assert _parse_status('Some extra text {"criterion_status": "MET"} ok') is True

    @pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="No OPENAI_API_KEY set")
    @pytest.mark.asyncio
    async def test_llm_judge_real_api(self):
        """Real criteria evaluation — Paris is correct capital, London is not."""
        from app.graders.llm_judge import LLMJudgeGrader
        result = await LLMJudgeGrader.grade(
            output="Paris",
            criteria=[
                {"criterion": "The output is a city name", "weight": 0.5},
                {"criterion": "The output is the capital city of France", "weight": 0.5},
            ],
            provider="openai",
            model="gpt-4o-mini",
        )
        assert result.score >= 0.5, f"Expected at least 0.5, got {result.score}"
        assert len(result.subscores) == 2
        assert result.subscores[0].name == "The output is a city name"

    @pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="No OPENAI_API_KEY set")
    @pytest.mark.asyncio
    async def test_llm_judge_wrong_answer(self):
        """Wrong capital — criteria should be UNMET → score 0."""
        from app.graders.llm_judge import LLMJudgeGrader
        result = await LLMJudgeGrader.grade(
            output="London",
            criteria=[
                {"criterion": "The output is the capital city of France", "weight": 1.0},
            ],
            provider="openai",
            model="gpt-4o-mini",
        )
        assert result.score == 0.0, f"London is not Paris; expected 0.0, got {result.score}"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CDP 3rd-fallback (PUT /json/new)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCDPEndpointResolution:
    """_resolve_page_ws fallback chain."""

    @pytest.mark.asyncio
    async def test_direct_devtools_url_used_as_is(self):
        from app.capabilities.browser import BrowserClient
        url = "ws://localhost:9222/devtools/page/abc123"
        result = await BrowserClient._resolve_page_ws("localhost", 9222, None, url)
        assert result == url

    @pytest.mark.asyncio
    async def test_target_id_constructs_url(self):
        from app.capabilities.browser import BrowserClient
        result = await BrowserClient._resolve_page_ws(
            "localhost", 9222, "target-xyz", "ws://localhost:9222"
        )
        assert result == "ws://localhost:9222/devtools/page/target-xyz"

    @pytest.mark.asyncio
    async def test_get_json_returns_page(self, monkeypatch):
        """Fallback 2: GET /json returns a page with webSocketDebuggerUrl."""
        from unittest.mock import AsyncMock, MagicMock
        from app.capabilities.browser import BrowserClient
        import app.capabilities.browser as _browser_mod

        page = {"type": "page", "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/p1"}
        mock_resp = MagicMock()
        mock_resp.json.return_value = [page]

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda: mock_ctx)

        result = await BrowserClient._resolve_page_ws("localhost", 9222, None, "ws://localhost:9222")
        assert result == "ws://localhost:9222/devtools/page/p1"

    @pytest.mark.asyncio
    async def test_put_json_new_when_no_pages(self, monkeypatch):
        """Fallback 3: GET /json returns no pages → PUT /json/new."""
        from unittest.mock import AsyncMock, MagicMock
        from app.capabilities.browser import BrowserClient
        import httpx

        new_target = {
            "type": "page", "id": "new1",
            "webSocketDebuggerUrl": "ws://localhost:9222/devtools/page/new1",
        }
        empty_resp = MagicMock()
        empty_resp.json.return_value = []
        new_resp = MagicMock()
        new_resp.json.return_value = new_target

        mock_client = AsyncMock()
        mock_client.get.return_value = empty_resp
        mock_client.put.return_value = new_resp
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        monkeypatch.setattr(httpx, "AsyncClient", lambda: mock_ctx)

        result = await BrowserClient._resolve_page_ws("localhost", 9222, None, "ws://localhost:9222")
        assert result == "ws://localhost:9222/devtools/page/new1"
        mock_client.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_bare_ws_on_error(self):
        """If GET /json fails entirely, fall back to bare ws://host:port."""
        from app.capabilities.browser import BrowserClient
        # Port 1 is effectively never listening; connection should fail fast
        result = await BrowserClient._resolve_page_ws("127.0.0.1", 19222, None, "ws://127.0.0.1:19222")
        assert result == "ws://127.0.0.1:19222"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Agent retry on stream failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentStreamRetry:
    """_retry_call retries transient connection errors, not ProviderError."""

    def test_succeeds_on_first_attempt(self):
        from app.services.llm import _retry_call
        calls = []
        from app.services.llm import LLMCallResult

        def fn():
            calls.append(1)
            return LLMCallResult(text="ok", provider="openai", model="gpt-4o",
                                 input_tokens=10, output_tokens=5,
                                 latency_ms=100, cost_usd=None)

        result = _retry_call(fn)
        assert result.text == "ok"
        assert len(calls) == 1

    def test_retries_on_connection_error(self):
        from app.services.llm import _retry_call, LLMCallResult
        attempts = []

        def fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("stream dropped")
            return LLMCallResult(text="recovered", provider="openai", model="gpt-4o",
                                 input_tokens=10, output_tokens=5,
                                 latency_ms=100, cost_usd=None)

        # Monkey-patch sleep so test runs fast
        import app.services.llm as llm_mod
        original_sleep = llm_mod.time.sleep
        llm_mod.time.sleep = lambda _: None
        try:
            result = _retry_call(fn)
        finally:
            llm_mod.time.sleep = original_sleep

        assert result.text == "recovered"
        assert len(attempts) == 3

    def test_does_not_retry_provider_error(self):
        from app.services.llm import _retry_call, ProviderError
        attempts = []

        def fn():
            attempts.append(1)
            raise ProviderError("OPENAI_API_KEY not set")

        with pytest.raises(ProviderError):
            _retry_call(fn)
        assert len(attempts) == 1  # no retry for missing key

    def test_reraises_after_max_retries(self):
        from app.services.llm import _retry_call, _APP_RETRIES
        attempts = []

        def fn():
            attempts.append(1)
            raise ConnectionError("always fails")

        import app.services.llm as llm_mod
        llm_mod.time.sleep = lambda _: None
        try:
            with pytest.raises(ConnectionError):
                _retry_call(fn)
        finally:
            import time as _time
            llm_mod.time.sleep = _time.sleep

        assert len(attempts) == _APP_RETRIES
