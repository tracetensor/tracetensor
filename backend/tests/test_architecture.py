"""
Architecture suite — the layering rules, enforced instead of documented.

Every rule here replaces a specific thing that had already gone wrong:

  * A router imported another router's private functions, so refactoring
    ingest.py would silently break dataset upload.
  * agent.py was 1,306 lines holding seven agents, and adding an eighth meant
    four edits scattered through it.
  * Status strings were bare literals in fifteen places, where a typo produced a
    row that no query would ever match again.
  * Business logic sat inline in route handlers, reachable only over HTTP.

These are static checks — import graph, module size, source scanning. They cost
nothing to run and fail the moment the structure starts eroding again.

Run:  cd backend && python tests/test_architecture.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
APP = BACKEND / "app"
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


def imports_of(path: Path) -> list[str]:
    """Every module this file imports, including function-local imports."""
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


# ---------------------------------------------------------------------------
print("\n== 1. Routers don't import each other ==")
# ---------------------------------------------------------------------------
ROUTERS = sorted((APP / "routers").glob("*.py"))
check("routers package is non-empty", len(ROUTERS) > 1, str(len(ROUTERS)))

for f in ROUTERS:
    # Leading-underscore modules in the package are shared helpers, not routers.
    if f.name.startswith("_"):
        continue
    bad = [
        m
        for m in imports_of(f)
        if m.startswith("app.routers.") and not m.rsplit(".", 1)[-1].startswith("_")
    ]
    check(f"{f.name} imports no sibling router", not bad, ", ".join(bad))


# ---------------------------------------------------------------------------
print("\n== 2. Services don't import routers (dependencies point one way) ==")
# ---------------------------------------------------------------------------
for f in sorted((APP / "services").rglob("*.py")):
    bad = [m for m in imports_of(f) if m.startswith("app.routers")]
    check(f"services/{f.relative_to(APP / 'services')} imports no router", not bad, ", ".join(bad))

for f in sorted((APP / "models").rglob("*.py")):
    bad = [m for m in imports_of(f) if m.startswith(("app.routers", "app.services"))]
    check(f"models/{f.name} stays persistence-only", not bad, ", ".join(bad))


# ---------------------------------------------------------------------------
print("\n== 3. No module is a god file ==")
# ---------------------------------------------------------------------------
# 600 lines is roughly what one person can hold in their head at once. The
# routers are the current ceiling; the limit exists to stop the next agent.py.
LINE_LIMIT = 600
oversized = []
for f in sorted(APP.rglob("*.py")):
    n = len(f.read_text().splitlines())
    if n > LINE_LIMIT:
        oversized.append(f"{f.relative_to(BACKEND)}={n}")
check(f"every module under {LINE_LIMIT} lines", not oversized, ", ".join(oversized))

agents_pkg = APP / "services" / "agents"
check("agents is a package, not one file", agents_pkg.is_dir())
check(
    "one module per agent",
    len(list(agents_pkg.glob("*.py"))) >= 6,
    str(sorted(p.name for p in agents_pkg.glob("*.py"))),
)
check("the old agent.py is gone", not (APP / "services" / "agent.py").exists())


# ---------------------------------------------------------------------------
print("\n== 4. Status values come from the enums, not string literals ==")
# ---------------------------------------------------------------------------
from app.models.enums import JobStatus, TaskStatus, TrialStatus  # noqa: E402

STATUS_LITERALS = {
    *JobStatus.values(),
    *TaskStatus.values(),
    # Only the trial states that don't collide with an ordinary English word;
    # "setup"/"error"/"completed" appear legitimately in prose and event payloads.
    TrialStatus.AGENT_RUNNING.value,
    TrialStatus.VERIFYING.value,
}
# Where a bare literal is genuinely correct rather than a missed replacement.
ALLOWED = {
    "app/models/enums.py",  # defines them
    "app/services/task_validator.py",  # re-exports STATUS_* for callers
    "app/schemas/common.py",  # publishes them to OpenAPI as Literal aliases
    "app/schemas/events.py",  # declares the SSE event + phase-status contract
}

offenders = []
for f in sorted(APP.rglob("*.py")):
    rel = str(f.relative_to(BACKEND))
    if rel in ALLOWED:
        continue
    tree = ast.parse(f.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and node.value in STATUS_LITERALS
        ):
            offenders.append(f"{rel}:{node.lineno} {node.value!r}")

check("no bare status literals in app/", not offenders, "; ".join(offenders[:6]))
check(
    "JobStatus.terminal() is the two end states",
    JobStatus.terminal() == {"completed", "failed"},
    str(sorted(JobStatus.terminal())),
)
check("enum members compare equal to their stored strings", JobStatus.QUEUED == "queued")

# The API's published contract must match the state machine exactly. A status the
# server can emit but the schema doesn't declare is a value clients can't handle;
# one the schema declares but the server never emits is a dead branch they'll
# write anyway.
from app.models.enums import PhaseStatus  # noqa: E402
from app.schemas.common import (  # noqa: E402
    JobStatusLiteral,
    TaskStatusLiteral,
    TrialStatusLiteral,
    literal_values,
)
from app.schemas.events import PhaseStatusLiteral  # noqa: E402

for _enum, _alias, _label in (
    (JobStatus, JobStatusLiteral, "JobStatus"),
    (TrialStatus, TrialStatusLiteral, "TrialStatus"),
    (TaskStatus, TaskStatusLiteral, "TaskStatus"),
    (PhaseStatus, PhaseStatusLiteral, "PhaseStatus"),
):
    check(
        f"{_label} is published exactly in the API schema",
        set(_enum.values()) == literal_values(_alias),
        f"diff={sorted(set(_enum.values()) ^ literal_values(_alias))}",
    )


# ---------------------------------------------------------------------------
print("\n== 5. Route handlers stay thin ==")
# ---------------------------------------------------------------------------
# A handler should validate, delegate, and shape a response. When one grows past
# this it has started doing the service's job again — which is exactly how
# start_examination reached 80 lines of inline orchestration.
HANDLER_LIMIT = 45
fat = []
for f in ROUTERS:
    tree = ast.parse(f.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_route = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr in {"get", "post", "put", "delete", "patch"}
            for d in node.decorator_list
        )
        if not is_route:
            continue
        # Count the handler's OWN statements, not code it merely defines. A
        # streaming endpoint's body is one inline async generator; that generator
        # is the response, not orchestration the service should own.
        nested = sum(
            (n.end_lineno or n.lineno) - n.lineno + 1
            for n in node.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        length = (node.end_lineno or node.lineno) - node.lineno - nested
        if length > HANDLER_LIMIT:
            fat.append(f"{f.name}:{node.name}={length}")
check(f"every route handler under {HANDLER_LIMIT} lines", not fat, ", ".join(fat))


# ---------------------------------------------------------------------------
print("\n== 6. Job creation is callable without HTTP ==")
# ---------------------------------------------------------------------------
from app.services import job_service  # noqa: E402

raises = {
    n.exc.func.id
    for n in ast.walk(ast.parse((APP / "services" / "job_service.py").read_text()))
    if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and isinstance(n.exc.func, ast.Name)
}
check(
    "job_service raises domain errors, not HTTPException",
    "HTTPException" not in raises,
    str(sorted(raises)),
)
check("create_job exists", callable(job_service.create_job))
check("create_dataset_run exists", callable(job_service.create_dataset_run))
check(
    "its errors are catchable as one family",
    issubclass(job_service.NotFound, job_service.JobServiceError)
    and issubclass(job_service.NotRunnable, job_service.JobServiceError)
    and issubclass(job_service.Conflict, job_service.JobServiceError),
)

# fastapi must not be reachable from the service layer at all — that's the
# difference between "a service" and "a handler that moved house".
for name in ("job_service.py", "task_service.py"):
    mods = imports_of(APP / "services" / name)
    check(
        f"{name} imports no web framework",
        not [m for m in mods if m.split(".")[0] in {"fastapi", "starlette"}],
    )


print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
