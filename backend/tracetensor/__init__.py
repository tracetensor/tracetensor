"""TraceTensor's public API — the surface that is safe to import.

`app.*` is the implementation and its internals move between releases. This
module is the stable facade `pyproject.toml` already promised (its
`packages.find` includes `tracetensor*` and points here) but that was never
actually written, so `import tracetensor` failed and the feature suite died on
it. Everything re-exported below is something an embedder legitimately needs:

    from tracetensor import run_trial, make_agent, make_environment

Two extension points are exported deliberately. `register_environment` and
`register_environment_lazy` let a third party add a sandbox backend without
forking — the lazy form is for backends whose vendor SDK should only be
imported if someone actually selects them.
"""

from __future__ import annotations

from app.core.config import settings
from app.services.agents import make_agent, resolve_agent
from app.services.agents.base import AgentResult, BaseAgent
from app.services.environment import (
    BaseEnvironment,
    ExecResult,
    available_backends,
    make_environment,
    register_environment,
    register_environment_lazy,
)
from app.services.trial_runner import run_trial

__version__ = settings.APP_VERSION

__all__ = [
    "AgentResult",
    "BaseAgent",
    "BaseEnvironment",
    "ExecResult",
    "__version__",
    "available_backends",
    "make_agent",
    "make_environment",
    "register_environment",
    "register_environment_lazy",
    "resolve_agent",
    "run_trial",
]
