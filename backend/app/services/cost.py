"""
USD cost — reported by the provider, never estimated by us.

TraceTensor always records tokens and latency. For dollars it reports only what
the agent's own tooling reports: Claude Code prints `total_cost_usd` for its run,
mini-swe reports its spend through litellm. Those are the provider's numbers.

There used to be a second source: a hardcoded `PRICING_USD_PER_1M` table that
multiplied tokens by per-model rates. It's gone, deliberately.

  * It covered 8 models and already missed gpt-5-codex, every Gemini model, and
    everything on OpenRouter — so most runs got no figure anyway.
  * Keeping it correct means tracking every provider's price changes forever.
  * A *wrong* number here is worse than no number. This is an evaluation
    platform; people compare agents on cost and publish the comparison. Silently
    stale rates would make those comparisons wrong in a way nobody could see.

For the built-in bash loop (a raw provider API, which reports usage but not
dollars) there is now no cost figure — only tokens. That's the honest answer:
Anthropic and OpenAI bill on tokens, so your invoice is authoritative and anyone
who wants dollars can multiply by the rate they actually pay.
"""

from __future__ import annotations

from typing import Optional

#: Retained so the CLI and UI can ask "should I render a $ column at all?".
#: True because vendor-reported cost is always recorded when an agent supplies
#: it — the flag no longer gates whether cost is *computed*, because nothing is
#: computed any more.
COST_TRACKING_ENABLED = True


def cost_enabled() -> bool:
    """Whether any cost figure can appear. See COST_TRACKING_ENABLED."""
    return COST_TRACKING_ENABLED


def normalize_cost(value: Optional[float]) -> Optional[float]:
    """Round a reported cost, preserving None.

    None means *unknown*, and that distinction is load-bearing all the way up:
    a trial's `cost_usd` total is None if any single call's cost is unknown, so
    a partial figure can never be presented as a complete one. Coercing None to
    0.0 anywhere here would silently understate a bill.
    """
    if value is None:
        return None
    return round(float(value), 6)
