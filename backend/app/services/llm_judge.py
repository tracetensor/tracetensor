"""
LLM-judge verifier — grade an agent's output with a model against a rubric.

For tasks with no clean pass/fail test (writing, summarizing, open-ended fixes),
a `test.sh` can't express "is this good". The LLM judge instead sends the task's
RUBRIC and the agent's OUTPUT to a model and asks for a score in [0, 1]. It's a
first-class verifier type, selected in task.toml:

  [verifier]
  type = "llm-judge"
  rubric = "Award 1.0 only if the summary is faithful and under 100 words."
  judge_model = "openai/gpt-4.1-mini"   # optional; provider/model
  judge_output_path = "/app/answer.md"  # optional; what to grade (else agent stdout)

The judge call's real cost is captured (an eval platform instruments its own
spend), and the model's reasoning is kept in the reward payload.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.services import llm
from app.services.cost import normalize_cost

DEFAULT_JUDGE_MODEL = "openai/gpt-4.1-mini"

_SYSTEM = (
    "You are a strict, fair grader. You are given a RUBRIC and an agent's OUTPUT. "
    "Score how well the OUTPUT satisfies the RUBRIC as a single number from 0.0 "
    "(does not meet it at all) to 1.0 (fully meets it). Be calibrated: partial "
    "credit is fine. Respond with ONLY a JSON object and nothing else: "
    '{"score": <number 0..1>, "reasoning": "<one or two sentences>"}.'
)

# Defensive caps — rubric is task-author content, output is agent-produced; keep
# the judge prompt bounded (cost + prompt-injection surface), like the bash agent.
MAX_RUBRIC_CHARS = 8_000
MAX_OUTPUT_CHARS = 24_000


@dataclass
class JudgeResult:
    reward: float
    passed: bool
    payload: dict
    log: str
    error: str | None = None


def _split_model(judge_model: str | None) -> tuple[str, str]:
    """'openai/gpt-4.1-mini' -> ('openai', 'gpt-4.1-mini'). A bare provider uses
    that provider's default model; a bare model assumes openai."""
    spec = judge_model or DEFAULT_JUDGE_MODEL
    if "/" in spec:
        provider, model = spec.split("/", 1)
    elif spec in llm.PROVIDERS:
        provider, model = spec, (llm.default_model(spec) or "")
    else:
        provider, model = "openai", spec
    return llm.canonical_provider(provider), model


def _parse_score(text: str) -> float | None:
    """Pull a [0,1] score from the model's reply (JSON preferred, then a bare number)."""
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1] if "{" in text else ""):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and isinstance(obj.get("score"), (int, float)):
                return max(0.0, min(1.0, float(obj["score"])))
        except (ValueError, TypeError):
            pass
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if m:
        return max(0.0, min(1.0, float(m.group())))
    return None


def judge(
    rubric: str,
    output: str,
    judge_model: str | None = None,
    pass_threshold: float = 1.0,
) -> JudgeResult:
    """Score `output` against `rubric` in [0, 1]. Never raises — a missing key or
    API error comes back as reward 0.0 with an error, so a trial still records."""
    rubric = (rubric or "").strip()[:MAX_RUBRIC_CHARS]
    output = (output or "").strip()[:MAX_OUTPUT_CHARS]
    if not rubric:
        return JudgeResult(0.0, False, {"error": "no rubric"}, "", error="llm-judge: no rubric set")
    provider, model = _split_model(judge_model)
    user = f"RUBRIC:\n{rubric}\n\nOUTPUT:\n{output or '(the agent produced no output)'}"
    try:
        call = llm.call_llm(provider, model, _SYSTEM, user, max_tokens=400)
    except llm.ProviderError as e:
        return JudgeResult(0.0, False, {"error": str(e)}, "", error=str(e))
    except Exception as e:  # SDK / network / API
        return JudgeResult(
            0.0, False, {"error": str(e)[:200]}, "", error=f"llm-judge call failed: {e}"
        )

    score = _parse_score(call.text)
    if score is None:
        return JudgeResult(
            0.0,
            False,
            {"error": "unparseable", "raw": call.text[:500]},
            call.text[:500],
            error="llm-judge: could not parse a score from the model reply",
        )
    payload = {
        "reward": score,
        "judge_model": f"{provider}/{model}",
        "raw": call.text[:500],
        # The judge's own spend — folds into the trial's cost summary like any call.
        "llm_call": {
            "provider": call.provider,
            "model": call.model,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "latency_ms": round(call.latency_ms, 1) if call.latency_ms is not None else None,
            "cost_usd": normalize_cost(call.cost_usd),
        },
    }
    return JudgeResult(
        reward=score,
        passed=score >= pass_threshold,
        payload=payload,
        log=f"[llm-judge {provider}/{model}] score={score}\n{call.text[:500]}",
    )
