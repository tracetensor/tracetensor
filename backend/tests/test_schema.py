"""
Schema-authority suite — Alembic is the only owner of the schema.

Three guarantees, all enforced here rather than by convention:

  1. PARITY   — `alembic upgrade head` produces the same schema as the ORM
                models. If someone changes a model and forgets the migration,
                this fails. That is the whole point of the suite.
  2. LEGACY   — a database created before dataset support / the durable queue /
                idempotency keys upgrades to head cleanly. This is what the old
                startup DDL shim used to do; the migration must still do it.
  3. NO SHIM  — nothing outside alembic/ issues DDL at startup.

Offline: SQLite temp files, no Docker, no server, no API key.

Run:  cd backend && python tests/test_schema.py
"""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
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


def _describe(db_path: str) -> dict:
    """Read a SQLite file's schema into a comparable structure.

    Compares column names + declared types and index names. Deliberately ignores
    auto-generated index names (sqlite_autoindex_*) and alembic's bookkeeping
    table, neither of which says anything about whether the two paths agree.
    """
    con = sqlite3.connect(db_path)
    try:
        tables = sorted(
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        )
        out = {}
        for t in tables:
            cols = {r[1]: r[2].upper() for r in con.execute(f"PRAGMA table_info({t})")}
            idx = sorted(
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
                    "AND name NOT LIKE 'sqlite_autoindex_%'",
                    (t,),
                )
            )
            out[t] = {"columns": cols, "indexes": idx}
        return out
    finally:
        con.close()


def _run_in_subprocess(code: str, db_path: str) -> subprocess.CompletedProcess:
    """Build a database in a fresh interpreter.

    Required, not incidental: app.core.config.Settings reads DATABASE_URL at
    import time, so two databases cannot be built from one process.
    """
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True
    )


_VIA_MIGRATIONS = """
import asyncio
from app.core.database import init_db
asyncio.run(init_db())
"""

_VIA_MODELS = """
import asyncio
from app.core.database import Base, engine, import_all_models
import_all_models()
async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
asyncio.run(main())
"""


# ---------------------------------------------------------------------------
print("\n== 1. Parity: `alembic upgrade head` == the ORM models ==")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    mig_db = str(Path(td) / "migrated.db")
    mod_db = str(Path(td) / "models.db")

    r1 = _run_in_subprocess(_VIA_MIGRATIONS, mig_db)
    check("init_db() ran migrations without error", r1.returncode == 0, r1.stderr[-300:])
    r2 = _run_in_subprocess(_VIA_MODELS, mod_db)
    check("create_all from models succeeded", r2.returncode == 0, r2.stderr[-300:])

    migrated = _describe(mig_db)
    models = _describe(mod_db)

    check(
        "same set of tables",
        set(migrated) == set(models),
        f"only-in-migrations={sorted(set(migrated) - set(models))} "
        f"only-in-models={sorted(set(models) - set(migrated))}",
    )

    for table in sorted(set(migrated) & set(models)):
        m_cols, o_cols = migrated[table]["columns"], models[table]["columns"]
        check(
            f"{table}: same columns",
            set(m_cols) == set(o_cols),
            f"diff={sorted(set(m_cols) ^ set(o_cols))}",
        )
        mismatched = {
            k: (m_cols[k], o_cols[k]) for k in set(m_cols) & set(o_cols) if m_cols[k] != o_cols[k]
        }
        check(f"{table}: same column types", not mismatched, str(mismatched))
        check(
            f"{table}: same indexes",
            set(migrated[table]["indexes"]) == set(models[table]["indexes"]),
            f"diff={sorted(set(migrated[table]['indexes']) ^ set(models[table]['indexes']))}",
        )

    con = sqlite3.connect(mig_db)
    stamped = [r[0] for r in con.execute("SELECT version_num FROM alembic_version")]
    con.close()
    check("migrated DB is stamped at a single head", len(stamped) == 1, str(stamped))


# ---------------------------------------------------------------------------
print("\n== 2. Legacy: a pre-migration database upgrades to head ==")
# ---------------------------------------------------------------------------
# A database as it looked before dataset support, the durable job queue, and
# idempotency keys — i.e. what the deleted startup shim existed to repair.
_LEGACY_SQL = """
CREATE TABLE tasks (id CHAR(32) PRIMARY KEY, name VARCHAR(255), created_at TIMESTAMP);
CREATE TABLE jobs (
  id CHAR(32) PRIMARY KEY, task_id CHAR(32), agent VARCHAR(50), model VARCHAR(120),
  n_trials INTEGER, status VARCHAR(30), trials_completed INTEGER, trials_passed INTEGER,
  pass_rate FLOAT, error TEXT, created_at TIMESTAMP, finished_at TIMESTAMP);
CREATE TABLE trials (id CHAR(32) PRIMARY KEY, job_id CHAR(32), task_id CHAR(32));
CREATE TABLE datasets (
  id CHAR(32) PRIMARY KEY, name VARCHAR(255), version VARCHAR(50),
  content_hash VARCHAR(64), task_ids JSON, task_names JSON, description TEXT,
  created_at TIMESTAMP);
CREATE TABLE dataset_runs (
  id CHAR(32) PRIMARY KEY, dataset_id CHAR(32), agent VARCHAR(50), model VARCHAR(120),
  n_trials INTEGER, concurrency INTEGER, status VARCHAR(30), error TEXT,
  created_at TIMESTAMP, finished_at TIMESTAMP);
"""

with tempfile.TemporaryDirectory() as td:
    legacy_db = str(Path(td) / "legacy.db")
    con = sqlite3.connect(legacy_db)
    con.executescript(_LEGACY_SQL)
    con.commit()
    con.close()

    r = _run_in_subprocess(_VIA_MIGRATIONS, legacy_db)
    check("legacy DB upgraded without error", r.returncode == 0, r.stderr[-400:])

    after = _describe(legacy_db)
    jobs = after.get("jobs", {"columns": {}, "indexes": []})
    runs = after.get("dataset_runs", {"columns": {}, "indexes": []})

    for col in (
        "concurrency",
        "worker_id",
        "claimed_at",
        "heartbeat_at",
        "dataset_run_id",
        "idempotency_key",
    ):
        check(f"jobs.{col} added", col in jobs["columns"])
    for col in ("worker_id", "claimed_at", "heartbeat_at", "idempotency_key"):
        check(f"dataset_runs.{col} added", col in runs["columns"])

    check("jobs idempotency key is UNIQUE", "uq_jobs_idempotency_key" in jobs["indexes"])
    check(
        "dataset_runs idempotency key is UNIQUE",
        "uq_dataset_runs_idempotency_key" in runs["indexes"],
    )
    check(
        "datasets (name, version) is UNIQUE",
        "uq_datasets_name_version" in after.get("datasets", {}).get("indexes", []),
    )
    for t in ("tasks", "jobs", "datasets", "dataset_runs"):
        check(
            f"{t}.created_at indexed", f"ix_{t}_created_at" in after.get(t, {}).get("indexes", [])
        )
    check("rate-limit ledger created", "_rate_limit_hits" in after)

    # Running it twice must be a no-op, not an error — every boot calls init_db().
    r2 = _run_in_subprocess(_VIA_MIGRATIONS, legacy_db)
    check("re-running migrations is idempotent", r2.returncode == 0, r2.stderr[-300:])


# ---------------------------------------------------------------------------
print("\n== 3. No second schema owner outside alembic/ ==")
# ---------------------------------------------------------------------------
db_src = (BACKEND / "app" / "core" / "database.py").read_text()
check("the startup DDL shim is gone", "def _ensure_additive_columns" not in db_src)
check("the alembic-stamping hack is gone", "def _stamp_alembic_if_needed" not in db_src)
check("database.py issues no DDL of its own", "exec_driver_sql" not in db_src)


def _code_strings(path: Path) -> list[str]:
    """Every string literal in a module except its docstrings.

    Parsing rather than grepping matters here: the modules that used to run DDL
    now *describe* that history in prose, and a plain text search would flag the
    explanation as the offence it warns about.
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


app_sql = []
for py in sorted((BACKEND / "app").rglob("*.py")):
    for literal in _code_strings(py):
        upper = literal.upper()
        for stmt in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX"):
            if stmt in upper:
                app_sql.append(f"{py.relative_to(BACKEND)}: {stmt}")
check("no executable DDL anywhere under app/", not app_sql, ", ".join(sorted(set(app_sql))))


print(f"\n=========== {passed} passed, {failed} failed ===========")
sys.exit(1 if failed else 0)
