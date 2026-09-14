"""Backend registry resolution and the recursive copy_in contract.

Both regressions these cover were silent: a backend registered eagerly made
module import order load-bearing, and a copy_in that skipped subdirectories
delivered a half-staged project whose failure surfaced later as a missing import.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app.services.environment import (
    BaseEnvironment,
    DockerEnvironment,
    available_backends,
    make_environment,
    register_environment_lazy,
)


class LazyBackendTests(unittest.TestCase):
    def _register(self, name: str, loader) -> None:
        """Register a backend and undo it — the registry is process-global, and a
        leaked test backend leaves every later caller of the backend catalog
        looking at a name it has no spec for."""
        from app.services import environment as env_mod

        register_environment_lazy(name, loader)
        self.addCleanup(env_mod._LAZY_BACKENDS.pop, name, None)
        self.addCleanup(env_mod._BACKENDS.pop, name, None)

    def test_daytona_is_listed_before_it_is_ever_built(self) -> None:
        self.assertIn("daytona", available_backends())

    def test_lazy_backend_is_not_imported_until_first_use(self) -> None:
        calls: list[int] = []

        class _Later(DockerEnvironment):
            pass

        def loader():
            calls.append(1)
            return _Later

        self._register("_test_lazy", loader)
        self.assertIn("_test_lazy", available_backends())
        self.assertEqual(calls, [], "loader ran at registration time")

        env = make_environment("_test_lazy", Path("/tmp/task"))
        self.assertIsInstance(env, _Later)
        self.assertEqual(calls, [1])

        # Resolved once, then promoted — a second build must not re-import.
        make_environment("_test_lazy", Path("/tmp/task"))
        self.assertEqual(calls, [1])

    def test_lazy_loader_returning_a_non_environment_is_rejected(self) -> None:
        self._register("_test_bad", lambda: str)
        with self.assertRaises(TypeError):
            make_environment("_test_bad", Path("/tmp/task"))

    def test_catalog_survives_a_backend_it_has_no_spec_for(self) -> None:
        """register_environment is the documented extension point, so the catalog
        must list a third-party backend rather than KeyError on the whole table."""
        from app.services.backend_catalog import list_backend_entries

        self._register("_test_thirdparty", lambda: DockerEnvironment)
        ids = [e.id for e in list_backend_entries()]
        self.assertIn("_test_thirdparty", ids)
        self.assertIn("docker", ids)

    def test_unknown_backend_still_lists_the_known_ones(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            make_environment("nope", Path("/tmp/task"))
        self.assertIn("docker", str(ctx.exception))


class ImportOrderTests(unittest.TestCase):
    def test_daytona_module_imports_standalone(self) -> None:
        """Importing the adapter first used to raise ImportError on a partially
        initialised module; only importing backend_catalog first made it work."""
        code = "import app.services.daytona_environment as d; print(d.DaytonaEnvironment.__name__)"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertIn("DaytonaEnvironment", proc.stdout)


class CopyInRecursionTests(unittest.TestCase):
    """DockerEnvironment.copy_in used to loop `iterdir()` filtering `is_file()`,
    so nested directories vanished without an error. Daytona's has always walked
    with rglob; these assert the two agree."""

    def _docker_env(self, task_dir: Path) -> DockerEnvironment:
        env = DockerEnvironment(task_dir)
        env.container_id = "cid123"  # _cid is a read-only guard over this
        return env

    def test_copies_directory_contents_in_one_recursive_command(self) -> None:
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "proj"
            (src / "tools").mkdir(parents=True)
            (src / "agent.py").write_text("x")
            (src / "tools" / "filesystem.py").write_text("y")

            env = self._docker_env(Path(tmp))
            with patch.object(env, "_run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
                env.copy_in(src, "/langgraph")

            cps = [c.args[0] for c in run.call_args_list if "cp" in c.args[0]]
            self.assertEqual(len(cps), 1, f"expected one recursive cp, got {cps}")
            # The trailing '/.' is what copies CONTENTS recursively rather than
            # nesting the directory itself under dest.
            self.assertTrue(cps[0][2].endswith("/."), cps[0])
            self.assertEqual(cps[0][3], "cid123:/langgraph")

    def test_copy_failure_raises_instead_of_staging_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "proj"
            src.mkdir()
            env = self._docker_env(Path(tmp))

            def _run(args, **kw):
                failed = MagicMock(returncode=1, stdout="", stderr="no such container")
                return failed if "cp" in args else MagicMock(returncode=0, stdout="", stderr="")

            with patch.object(env, "_run", side_effect=_run):
                with self.assertRaises(RuntimeError) as ctx:
                    env.copy_in(src, "/langgraph")
        self.assertIn("no such container", str(ctx.exception))

    def test_missing_source_directory_is_named(self) -> None:
        with TemporaryDirectory() as tmp:
            env = self._docker_env(Path(tmp))
            with self.assertRaises(RuntimeError) as ctx:
                env.copy_in(Path(tmp) / "does-not-exist", "/dest")
        self.assertIn("not a directory", str(ctx.exception))

    def test_both_backends_declare_copy_in(self) -> None:
        self.assertTrue(hasattr(BaseEnvironment, "copy_in"))
        from app.services.daytona_environment import DaytonaEnvironment

        for cls in (DockerEnvironment, DaytonaEnvironment):
            self.assertIn("copy_in", cls.__dict__, cls.__name__)


if __name__ == "__main__":
    unittest.main()
