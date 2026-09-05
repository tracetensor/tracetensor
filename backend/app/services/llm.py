"""
LLM utilities — one place to switch between providers and models.

SDK-native (uses the `anthropic` and `openai` SDKs directly; OpenRouter is the
OpenAI SDK pointed at OpenRouter's base URL). Mirrors the spirit of a shared
llm_utils: a model catalog per provider, per-provider request handling, and a
single `call_llm(provider, model, system, user)` entry point the agent uses.

Keys are read from the environment (loaded from backend/.env at startup).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

# Importing config loads backend/.env into the environment, so the os.getenv key
# reads below work even when nothing else has touched settings yet (e.g. the
# built-in LLM loop or the llm-judge verifier driven straight through run_trial).
from app.core import config as _config  # noqa: F401
from app.core.logging import get_logger

log = get_logger("tracetensor.llm")

# Providers we support. "claude" is accepted as an alias for "anthropic".
PROVIDERS = ("anthropic", "openai", "openrouter")
PROVIDER_ALIASES = {"claude": "anthropic", "gpt": "openai"}

# Provider -> the name of its API key. Doubles as the env var name AND the
# Settings attribute, because config.py reads each `os.getenv(NAME)` into
# `self.NAME`. This is the single source for both; the agents package aliases it
# as LITELLM_PROVIDER_KEY_ATTR rather than keeping its own copy.
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# OpenAI reasoning families need max_completion_tokens and reject temperature/
# a system role — we handle them specially in _call_openai_compatible.
OPENAI_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")

# ----------------------------------------------------------------------
# Model catalog — drives the UI dropdown. Edit these to taste; a model only
# works if your account/key has access to it (a bad id surfaces as an API error).
# ----------------------------------------------------------------------
MODELS: Dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "default": "claude-opus-4-8",
        "options": [
            {"id": "claude-opus-4-8", "label": "Claude Opus 4.8 · best"},
            {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 · balanced"},
            {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5 · fast"},
        ],
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "default": "gpt-4o",
        "options": [
            {"id": "gpt-4o", "label": "GPT-4o · best"},
            {"id": "gpt-4.1", "label": "GPT-4.1"},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 mini · fast"},
            {"id": "gpt-5", "label": "GPT-5 · reasoning"},
            {"id": "gpt-4o-mini", "label": "GPT-4o mini · fast"},
        ],
    },
    "openrouter": {
        "label": "OpenRouter",
        "default": "anthropic/claude-3.7-sonnet",
        # OpenRouter ids change often — adjust to your preferred models.
        "options": [
            {"id": "anthropic/claude-3.7-sonnet", "label": "Claude 3.7 Sonnet (OR)"},
            {"id": "openai/gpt-4o", "label": "GPT-4o (OR)"},
            {"id": "deepseek/deepseek-chat", "label": "DeepSeek Chat (OR)"},
            {"id": "google/gemini-2.0-flash-001", "label": "Gemini 2.0 Flash (OR)"},
        ],
    },
}


# The built-in bash loop calls a raw provider API, which reports tokens and
# latency but not dollars. We don't multiply tokens by a rate table any more (see
# app.services.cost for why), so cost_usd is None here — tokens are the honest,
# complete figure. Installed agents that run their own tooling DO report a real
# dollar cost, and that flows through normalize_cost unchanged.


@dataclass
class LLMCallResult:
    """What one call_llm() invocation produced, for cost/latency instrumentation
    — an eval platform has to watch its own LLM spend and behavior."""

    text: str
    provider: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: Optional[float] = None
    cost_usd: Optional[float] = None
    # True when the response had thinking blocks but the text block was empty —
    # the model thought but produced no command. The agent loop can retry with a
    # prompt rather than silently treating this as DONE.
    thinking_only: bool = False


class ProviderError(RuntimeError):
    """Raised when a provider can't be reached (missing key/package)."""


def canonical_provider(name: str) -> str:
    return PROVIDER_ALIASES.get(name, name)


def default_model(provider: str) -> Optional[str]:
    return MODELS.get(canonical_provider(provider), {}).get("default")


def catalog(available: Dict[str, bool]) -> List[dict]:
    """Return the provider/model catalog for the UI, flagged by key availability."""
    out = []
    for pid in PROVIDERS:
        spec = MODELS[pid]
        out.append(
            {
                "id": pid,
                "label": spec["label"],
                "available": bool(available.get(pid)),
                "requires_key": PROVIDER_KEY_ENV[pid],
                "default_model": spec["default"],
                "models": spec["options"],
            }
        )
    return out


# ----------------------------------------------------------------------
# Provider calls — each returns the model's text response.
# ----------------------------------------------------------------------
def _key(provider: str) -> str:
    env = PROVIDER_KEY_ENV[provider]
    val = os.getenv(env)
    if not val:
        raise ProviderError(f"{env} not set.")
    return val


# Per-call bounds so one hung/slow provider call can't freeze a trial past its
# agent timeout. The SDKs' own default is ~10 min, which is far too loose here.
LLM_TIMEOUT_SEC = 120.0
LLM_MAX_RETRIES = 2  # SDK-level retries on transient errors (429 / 5xx / connection)


def _call_anthropic(model: str, system: str, user: str, max_tokens: int) -> LLMCallResult:
    try:
        import anthropic
    except ImportError:
        raise ProviderError("anthropic package not installed (pip install anthropic).")
    client = anthropic.Anthropic(
        api_key=_key("anthropic"),
        timeout=LLM_TIMEOUT_SEC,
        max_retries=LLM_MAX_RETRIES,
    )
    t0 = time.monotonic()
    # No temperature: current Claude models (Opus 4.8 / Sonnet 5 / …) reject it.
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    latency_ms = (time.monotonic() - t0) * 1000
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    # Detect thinking-only response: model spent tokens thinking but emitted no
    # command text. The agent loop uses thinking_only=True to retry with an
    # explicit "now output ONE bash command" nudge instead of silently breaking.
    has_thinking = any(getattr(b, "type", "") == "thinking" for b in msg.content)
    thinking_only = not text and has_thinking
    in_tok = getattr(msg.usage, "input_tokens", None)
    out_tok = getattr(msg.usage, "output_tokens", None)
    return LLMCallResult(
        text=text,
        provider="anthropic",
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=latency_ms,
        cost_usd=None,  # raw API reports no dollar cost — see the note above
        thinking_only=thinking_only,
    )


def _call_openai_compatible(
    model: str, system: str, user: str, max_tokens: int, *, provider: str, base_url: Optional[str]
) -> LLMCallResult:
    try:
        from openai import OpenAI
    except ImportError:
        raise ProviderError("openai package not installed (pip install openai).")
    key = _key(provider)
    # Pass args explicitly (not a **dict splat) so each is type-checked against
    # the SDK's real parameter types. base_url is only set for OpenRouter et al.
    if base_url:
        client = OpenAI(
            api_key=key, timeout=LLM_TIMEOUT_SEC, max_retries=LLM_MAX_RETRIES, base_url=base_url
        )
    else:
        client = OpenAI(api_key=key, timeout=LLM_TIMEOUT_SEC, max_retries=LLM_MAX_RETRIES)

    bare = model.split("/")[-1]  # OpenRouter ids look like "openai/gpt-4o"
    is_reasoning = bare.startswith(OPENAI_REASONING_PREFIXES)
    t0 = time.monotonic()
    if is_reasoning:
        # Reasoning models: merge system into the prompt, use max_completion_tokens,
        # and don't send temperature (only the default is accepted).
        resp = client.chat.completions.create(
            model=model,
            max_completion_tokens=max(max_tokens, 2000),
            messages=[{"role": "user", "content": system + "\n\n" + user}],
        )
    else:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    latency_ms = (time.monotonic() - t0) * 1000
    text = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "prompt_tokens", None) if usage else None
    out_tok = getattr(usage, "completion_tokens", None) if usage else None
    if is_reasoning and not text and (out_tok or 0) > 0:
        log.warning(
            "reasoning_empty_content",
            extra={"model": bare, "completion_tokens": out_tok, "provider": provider},
        )
    return LLMCallResult(
        text=text,
        provider=provider,
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=latency_ms,
        cost_usd=None,  # raw API reports no dollar cost — see the note above
    )


def call_llm(
    provider: str, model: str, system: str, user: str, max_tokens: int = 800
) -> LLMCallResult:
    """Single entry point. Raises ProviderError on missing key/package; other
    SDK/API errors propagate to the caller. Returns tokens/latency/cost
    alongside the text — an eval platform has to instrument its own LLM spend,
    not just forward the model's answer."""
    provider = canonical_provider(provider)
    if provider == "anthropic":
        result = _call_anthropic(model, system, user, max_tokens)
    elif provider == "openai":
        result = _call_openai_compatible(
            model, system, user, max_tokens, provider="openai", base_url=None
        )
    elif provider == "openrouter":
        result = _call_openai_compatible(
            model,
            system,
            user,
            max_tokens,
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
        )
    else:
        raise ProviderError(f"Unknown provider: {provider}")

    log.info(
        "llm_call",
        extra={
            "provider": result.provider,
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "latency_ms": round(result.latency_ms, 1) if result.latency_ms is not None else None,
            "cost_usd": result.cost_usd,
        },
    )
    return result
