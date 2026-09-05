"""What each execution backend can actually enforce, checked before provisioning.

A backend that cannot honour part of a task has always failed — but it failed
*during* the trial, after a sandbox was created and sometimes after the agent had
already spent money. The task was never runnable there; only the discovery was
expensive.

So each backend declares its capabilities and the task is checked against them up
front. Every field below exists because there is a rule that reads it: a flag
nobody validates is a claim nobody checks, which is how a capability list turns
into documentation that quietly goes stale.

Defaults are all False. A new backend claims nothing until it implements
something, so the failure mode of forgetting to declare is "refused with a clear
message", not "accepted and silently unenforced".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.task import TaskConfig


@dataclass(frozen=True)
class BackendCapabilities:
    network_isolation: bool = False
    """Can run a task with no network access at all (`network_mode = "no-network"`)."""

    network_allowlist: bool = False
    """Can restrict egress to an allowlist (`network_mode = "allowlist"`)."""

    dynamic_network: bool = False
    """Can change network policy after the sandbox has started.

    Required by a task whose `[agent]` or `[verifier]` network_mode differs from
    the `[environment]` baseline: the switch happens between phases, in a live
    sandbox. A backend without this can still run the task's baseline policy.
    """

    separate_verifier: bool = False
    """Can move the agent's artifacts into a second, isolated grading sandbox
    (`[verifier] environment_mode = "separate"`)."""


def capabilities_for(backend: str) -> BackendCapabilities:
    """What `backend` claims it can do. Unknown backends claim nothing."""
    from app.services.environment import _resolve_backend

    cls = _resolve_backend(backend)
    if cls is None:
        return BackendCapabilities()
    return cls.capabilities()


def unsupported_features(backend: str, cfg: TaskConfig) -> list[str]:
    """Reasons this task cannot run on this backend. Empty means it can.

    Each message names the task setting, the backend, and what to do — this is
    read by someone who just had a run refused and needs to know whether to
    change the task or change the backend.
    """
    caps = capabilities_for(backend)
    baseline = cfg.environment.network_mode
    problems: list[str] = []

    if baseline == "no-network" and not caps.network_isolation:
        problems.append(
            f"[environment] network_mode = 'no-network' — the {backend} backend cannot "
            "enforce network isolation, and running with egress would give the agent "
            "more access than the task allows."
        )
    if baseline == "allowlist" and not caps.network_allowlist:
        problems.append(
            f"[environment] network_mode = 'allowlist' — the {backend} backend cannot "
            "restrict egress to an allowlist. Use 'public' or 'no-network'."
        )

    if not caps.dynamic_network:
        for phase, mode in (
            ("agent", cfg.agent.network_mode),
            ("verifier", cfg.verifier.network_mode),
        ):
            if mode and mode != baseline:
                problems.append(
                    f"[{phase}] network_mode = '{mode}' differs from the [environment] "
                    f"baseline '{baseline}', but the {backend} backend cannot change "
                    "network policy after the sandbox starts. Remove the phase override "
                    "or run on a backend that supports switching."
                )

    if cfg.verifier.environment_mode == "separate" and not caps.separate_verifier:
        problems.append(
            f"[verifier] environment_mode = 'separate' — the {backend} backend cannot "
            "move artifacts into an isolated grading sandbox. Use the shared verifier."
        )

    return problems
