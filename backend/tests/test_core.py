"""
Core-logic tests: parser + validator + storage.

Runs WITHOUT FastAPI/SQLAlchemy/real-pydantic so it works in the offline
sandbox. It installs a pydantic shim, then imports the real service modules
and exercises them against the real examples/sort-csv task.
"""

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]  # .../backend
PROJECT = BACKEND.parent  # .../tracetensor

sys.path.insert(0, str(BACKEND))  # so `import app...` works
sys.path.insert(0, str(HERE.parent))

import _pydantic_shim  # noqa: E402

_pydantic_shim.install()

from app.services.task_parser import TaskParseError, parse_task_toml  # noqa: E402
from app.services.task_validator import STATUS_READY, STATUS_REGISTERED, validate_task  # noqa: E402
from app.storage.task_store import (  # noqa: E402
    read_task_file,
    safe_task_name,
    store_task_from_files,
    store_task_from_zip,
)

EXAMPLE = PROJECT / "examples" / "sort-csv"

_passed = 0
_failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}")


print("\n== 1. Parser: real task.toml ==")
cfg = parse_task_toml((EXAMPLE / "task.toml").read_bytes())
check("name parsed", cfg.name == "tracetensor/sort-csv")
check("description parsed", "revenue" in (cfg.description or ""))
check("schema_version", cfg.schema_version == "1.3")
check("author parsed", len(cfg.authors) == 1 and cfg.authors[0].name == "Rahul")
check("keywords parsed", "csv" in cfg.keywords)
check("category from metadata", cfg.category == "programming")
check("verifier timeout", cfg.verifier.timeout_sec == 60.0)
check("agent timeout", cfg.agent.timeout_sec == 120.0)
check("env network_mode", cfg.environment.network_mode == "no-network")
check("env os default linux", cfg.environment.os == "linux")


print("\n== 2. Parser: malformed input ==")
try:
    parse_task_toml(b"this is not toml = = =")
    check("rejects bad toml", False)
except TaskParseError:
    check("rejects bad toml", True)

try:
    parse_task_toml(b'[task]\ndescription = "no name"\n')
    check("rejects missing name", False)
except TaskParseError:
    check("rejects missing name", True)

try:
    parse_task_toml(b'[task]\nname="x"\n[environment]\nos="mac"\n')
    check("rejects bad os value", False)
except TaskParseError:
    check("rejects bad os value", True)


print("\n== 2b. Parser: Terminal-Bench 1.0 legacy ==")
TB_SAMPLE = b"""version = "1.0"

[metadata]
author_name = "Nicholas Carlini"
author_email = "nicholas@carlini.com"
difficulty = "hard"
category = "software-engineering"
tags = ["c", "ml"]

[verifier]
timeout_sec = 900.0

[agent]
timeout_sec = 900.0

[environment]
build_timeout_sec = 600.0
docker_image = "alexgshaw/gpt2-codegolf:20251031"
cpus = 1
memory = "4G"
storage = "10G"
"""
cfg_tb = parse_task_toml(TB_SAMPLE, Path("gpt2-codegolf"))
check("TB name from task_dir", cfg_tb.name == "gpt2-codegolf")
check("TB author parsed", cfg_tb.authors[0].name == "Nicholas Carlini")
check("TB memory 4G -> 4096 MB", cfg_tb.environment.memory_mb == 4096)
check("TB storage 10G -> 10240 MB", cfg_tb.environment.storage_mb == 10240)
check("TB category", cfg_tb.category == "software-engineering")
check("TB difficulty_explanation", cfg_tb.difficulty_explanation == "hard")


print("\n== 3. Validator: valid task ==")
res = validate_task(EXAMPLE)
check("is_valid", res.is_valid is True)
check("status ready", res.status == STATUS_READY)
check("no errors", res.errors == [])
check("has_instruction", res.has_instruction)
check("has_task_toml", res.has_task_toml)
check("has_dockerfile", res.has_dockerfile)
check("has_test_script", res.has_test_script)
check("has_solution", res.has_solution)
check("no warnings (solution present)", res.warnings == [])


print("\n== 4. Validator: broken tasks ==")
with tempfile.TemporaryDirectory() as td:
    broken = Path(td) / "broken"
    broken.mkdir()
    r = validate_task(broken)
    check("empty dir invalid", not r.is_valid)
    check("empty dir status registered", r.status == STATUS_REGISTERED)
    check("reports missing instruction", any("instruction" in e for e in r.errors))
    check("reports missing task.toml", any("task.toml" in e for e in r.errors))
    check("reports missing environment", any("environment" in e for e in r.errors))
    check("reports missing tests", any("test.sh" in e or "tests" in e for e in r.errors))

with tempfile.TemporaryDirectory() as td:
    # Missing solution only -> valid but warns.
    p = Path(td) / "nosol"
    (p / "environment").mkdir(parents=True)
    (p / "tests").mkdir(parents=True)
    (p / "instruction.md").write_text("do a thing")
    (p / "task.toml").write_text('[task]\nname="x/y"\n')
    (p / "environment" / "Dockerfile").write_text("FROM python:3.11")
    (p / "tests" / "test.sh").write_text("echo hi")
    r = validate_task(p)
    check("valid without solution", r.is_valid)
    check("warns about solution", any("solve.sh" in w for w in r.warnings))

with tempfile.TemporaryDirectory() as td:
    # docker_image in task.toml, no Dockerfile -> still valid environment.
    p = Path(td) / "imgonly"
    (p / "tests").mkdir(parents=True)
    (p / "instruction.md").write_text("x")
    (p / "task.toml").write_text('[task]\nname="x/y"\n[environment]\ndocker_image="python:3.11"\n')
    (p / "tests" / "test.sh").write_text("echo hi")
    r = validate_task(p)
    check("docker_image counts as environment", r.is_valid)
    check("has_docker_image flag set", r.has_docker_image)


print("\n== 5. Storage: name safety ==")
check("slash replaced", safe_task_name("myorg/sort-csv") == "myorg__sort-csv")


print("\n== 6. Storage: store from files + read back ==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    files = {
        "instruction.md": b"hello",
        "task.toml": b'[task]\nname="a/b"\n',
        "environment/Dockerfile": b"FROM python:3.11",
        "tests/test.sh": b"echo hi",
    }
    d = store_task_from_files(root, "a/b", files)
    check("dir created", d.exists())
    check("nested file created", (d / "environment" / "Dockerfile").exists())
    check("read_task_file works", read_task_file(d, "instruction.md") == "hello")
    check("missing file -> None", read_task_file(d, "nope.md") is None)


print("\n== 7. Storage: store from ZIP (with top-level folder) ==")
import io
import zipfile  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sort-csv/instruction.md", "hi")
        zf.writestr("sort-csv/task.toml", '[task]\nname="z/z"\n')
        zf.writestr("sort-csv/environment/Dockerfile", "FROM python:3.11")
        zf.writestr("sort-csv/tests/test.sh", "echo hi")
    d = store_task_from_zip(root, "z/z", buf.getvalue())
    # Top-level "sort-csv/" should have been stripped.
    check("zip: instruction at root", (d / "instruction.md").exists())
    check("zip: dockerfile nested", (d / "environment" / "Dockerfile").exists())
    check("zip: no leftover top folder", not (d / "sort-csv").exists())
    r = validate_task(d)
    check("zip-extracted task validates (warns no sol)", r.is_valid)


print("\n== 8. Storage: reject zip-slip path ==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
    try:
        store_task_from_zip(root, "evil", buf.getvalue())
        # If the single-folder strip logic ate it, ensure nothing escaped.
        escaped = (root.parent / "evil.txt").exists()
        check("no path escape", not escaped)
    except ValueError:
        check("no path escape", True)


print("\n== 9. Storage: C-1 path-traversal via task name ==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    sentinel = root.parent / "PWNED"
    sentinel.unlink(missing_ok=True)

    # A bare '..' name must be rejected before rmtree/mkdir touches parent dir.
    try:
        store_task_from_files(root, "..", {"instruction.md": b"x"})
        check("'..' name rejected", False)
    except ValueError:
        check("'..' name rejected", True)

    # '../../etc' — safe_task_name turns slashes into '__', producing the literal
    # directory name '..____..____etc' which is inside the root (not an escape).
    d2 = store_task_from_files(root, "../../etc", {"instruction.md": b"x"})
    check("'../../etc' safe after slash-replace", root in d2.parents)

    # Legitimate names must still work.
    d = store_task_from_files(root, "myorg/task", {"instruction.md": b"ok"})
    check("normal name works", d.exists())
    check("normal name inside root", root in d.parents)
    check("PWNED file not created", not sentinel.exists())


print(f"\n=========== {_passed} passed, {_failed} failed ===========")
sys.exit(1 if _failed else 0)
