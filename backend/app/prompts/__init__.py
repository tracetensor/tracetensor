"""Prompts, versioned like code.

Each prompt lives in its own module with an explicit version string. Bump the
version whenever the wording changes materially — it travels with every
trial's trajectory, so a score change can be attributed to "the model got
worse" vs. "we changed the prompt" instead of being ambiguous.
"""
