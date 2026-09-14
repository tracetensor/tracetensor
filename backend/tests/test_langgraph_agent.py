"""Offline tests for the LangGraph adapter (no sandbox, no model calls).

Covers the parts that fail silently in a real run: a result record we can't find
reports zero spend for a trial that cost money, and a project staged in the wrong
order installs dependencies that aren't there yet.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from app.services.agents.langgraph_agent import (
    BEGIN,
    END,
    PROJECT_DIR,
    LangGraphAgent,
    _parse_result,
)
from app.services.agents.registry import langgraph_key_check, lookup


def _project(tmp: str, registry: dict | None = None) -> Path:
    p = Path(tmp) / "proj"
    p.mkdir()
    (p / "langgraph.json").write_text(
        json.dumps(registry or {"dependencies": ["."], "graphs": {"a": "./agent.py:make_agent"}})
    )
    (p / "agent.py").write_text("def make_agent():\n    return None\n")
    return p


class ResultParsingTests(unittest.TestCase):
    def test_finds_the_record_amid_framework_noise(self) -> None:
        stdout = (
            "UserWarning: tokenizers parallelism disabled\n"
            f"{BEGIN}\n"
            '{"ok": true, "usage": {"api_calls": 3, "input_tokens": 10, "output_tokens": 4}}\n'
            f"{END}\n"
            "some trailing chatter\n"
        )
        parsed = _parse_result(stdout)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["usage"]["api_calls"], 3)

    def test_missing_markers_is_none_not_a_guess(self) -> None:
        self.assertIsNone(_parse_result("no markers here"))
        self.assertIsNone(_parse_result(""))
        self.assertIsNone(_parse_result(None))

    def test_unparseable_body_is_kept_as_raw(self) -> None:
        parsed = _parse_result(f"{BEGIN}\nnot json\n{END}")
        self.assertEqual(parsed["raw"], "not json")


class SpendRollupTests(unittest.TestCase):
    def _agent(self, tmp) -> LangGraphAgent:
        return LangGraphAgent(str(_project(tmp)), "anthropic/claude-haiku-4-5")

    def test_usage_becomes_one_run_scoped_record(self) -> None:
        with TemporaryDirectory() as tmp:
            calls = self._agent(tmp)._llm_calls(
                {"usage": {"api_calls": 4, "input_tokens": 5325, "output_tokens": 618,
                           "model": "claude-haiku-4-5-20251001"}}
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["api_calls"], 4)
            self.assertEqual(calls[0]["input_tokens"], 5325)
            self.assertEqual(calls[0]["provider"], "anthropic")

    def test_no_usage_reports_nothing_rather_than_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            self.assertEqual(agent._llm_calls(None), [])
            self.assertEqual(agent._llm_calls({"usage": {"api_calls": 0}}), [])


class ProjectResolutionTests(unittest.TestCase):
    def test_constructs_without_a_project_configured(self) -> None:
        """The registry builds one of every agent to report status, so construction
        must never depend on an env var. The project is resolved in `run`."""
        with patch("app.services.agents.langgraph_agent.settings") as s:
            s.LANGGRAPH_PROJECT = None
            s.LANGGRAPH_GRAPH = None
            agent = LangGraphAgent(None, "anthropic/claude-haiku-4-5")
        self.assertIsNone(agent.project)

    def test_missing_project_is_a_trial_error_naming_the_variable(self) -> None:
        with patch("app.services.agents.langgraph_agent.settings") as s:
            s.LANGGRAPH_PROJECT = None
            s.LANGGRAPH_GRAPH = None
            agent = LangGraphAgent(None, "anthropic/claude-haiku-4-5")
            result = agent.run("x", MagicMock(workdir="/app"), timeout=5)
        self.assertIn("LANGGRAPH_PROJECT", result.error)

    def test_directory_without_a_registry_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = LangGraphAgent(tmp, "anthropic/claude-haiku-4-5")
            result = agent.run("x", MagicMock(workdir="/app"), timeout=5)
        self.assertIn("langgraph.json", result.error)

    def test_dot_dependency_is_skipped_but_real_ones_install(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = _project(tmp, {"dependencies": [".", "langchain-anthropic", "httpx"],
                                  "graphs": {"a": "./agent.py:make_agent"}})
            agent = LangGraphAgent(str(proj), "anthropic/claude-haiku-4-5")
            agent._resolve_project()
            self.assertEqual(agent._declared_dependencies(), ["langchain-anthropic", "httpx"])
            self.assertIn("langgraph", agent.INSTALL)
            self.assertIn("httpx", agent.INSTALL)

    def test_model_reaches_the_graph_as_a_bare_id(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = LangGraphAgent(str(_project(tmp)), "anthropic/claude-haiku-4-5")
            env = MagicMock(workdir="/app")
            self.assertEqual(agent._extra_env(env)["TT_MODEL"], "claude-haiku-4-5")
            self.assertEqual(agent._extra_env(env)["TT_WORKDIR"], "/app")


class StagingOrderTests(unittest.TestCase):
    def test_project_is_staged_before_install_runs(self) -> None:
        """INSTALL may pip-install the project's own declared deps, so the copy
        has to land first. Asserted on call order, not just that both happened."""
        with TemporaryDirectory() as tmp:
            agent = LangGraphAgent(str(_project(tmp)), "anthropic/claude-haiku-4-5")
            env = MagicMock(workdir="/app")
            order: list[str] = []
            env.copy_in.side_effect = lambda *a, **k: order.append("copy_in")
            env.write_file.side_effect = lambda *a, **k: order.append("write_file")
            env.exec.side_effect = lambda cmd, **k: (
                order.append("exec:install" if "pip" in cmd or "PIP" in cmd else "exec:other"),
                MagicMock(exit_code=0, stdout="", stderr=""),
            )[1]
            with patch.object(agent, "_secret_env", return_value={"ANTHROPIC_API_KEY": "x"}):
                agent.run("do the thing", env, timeout=30)

            self.assertLess(order.index("copy_in"), order.index("exec:install"))
            env.copy_in.assert_called_once()
            self.assertEqual(env.copy_in.call_args[0][1], PROJECT_DIR)

    def test_staging_failure_is_reported_as_an_agent_error_not_a_crash(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = LangGraphAgent(str(_project(tmp)), "anthropic/claude-haiku-4-5")
            env = MagicMock(workdir="/app")
            env.copy_in.side_effect = RuntimeError("sandbox went away")
            result = agent.run("x", env, timeout=5)
        self.assertIn("Could not stage", result.error)
        self.assertIn("sandbox went away", result.error)


class GraphFactoryShapeTests(unittest.TestCase):
    """A langgraph.json ref can point at six legitimate shapes. Calling every one
    with no arguments broke `def make_graph(config)` — LangGraph's own deployment
    convention — so each shape gets a case."""

    @staticmethod
    def _runner():
        import importlib.util

        path = Path(__file__).parent.parent / "app/services/agents/_langgraph_runner.py"
        spec = importlib.util.spec_from_file_location("_tt_runner_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _run(self, target):
        import asyncio

        cfg = {"recursion_limit": 50, "configurable": {"model": "claude-haiku-4-5"}}
        return asyncio.run(self._runner()._resolve_and_invoke(target, {"messages": []}, cfg))

    def setUp(self) -> None:
        class Graph:
            def __init__(self, tag):
                self.tag = tag

            def invoke(self, payload, config=None):
                return {"messages": [], "tag": self.tag, "config": config}

        class AsyncGraph:
            def __init__(self, tag):
                self.tag = tag

            async def ainvoke(self, payload, config=None):
                return {"messages": [], "tag": self.tag, "config": config}

        self.Graph, self.AsyncGraph = Graph, AsyncGraph

    def test_no_arg_factory(self) -> None:
        self.assertEqual(self._run(lambda: self.Graph("noarg"))["tag"], "noarg")

    def test_config_taking_factory_receives_the_run_model(self) -> None:
        def make_graph(config):
            return self.Graph(config["configurable"]["model"])

        self.assertEqual(self._run(make_graph)["tag"], "claude-haiku-4-5")

    def test_async_factory(self) -> None:
        async def make_graph(config):
            return self.Graph("async")

        self.assertEqual(self._run(make_graph)["tag"], "async")

    def test_sync_context_manager_stays_open_across_invoke(self) -> None:
        import contextlib

        closed: list[bool] = []

        @contextlib.contextmanager
        def make_graph(config):
            try:
                yield self.Graph("sync_cm")
            finally:
                closed.append(True)

        self.assertEqual(self._run(make_graph)["tag"], "sync_cm")
        self.assertEqual(closed, [True], "context manager was not closed")

    def test_async_context_manager(self) -> None:
        import contextlib

        @contextlib.asynccontextmanager
        async def make_graph(config):
            yield self.AsyncGraph("async_cm")

        self.assertEqual(self._run(make_graph)["tag"], "async_cm")

    def test_prebuilt_graph_is_used_as_is(self) -> None:
        self.assertEqual(self._run(self.Graph("prebuilt"))["tag"], "prebuilt")

    def test_non_callable_non_graph_is_a_clear_error(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self._run(42)
        self.assertIn("neither an invokable graph nor a factory", str(ctx.exception))


class RegistryTests(unittest.TestCase):
    def test_registered_under_name_and_aliases(self) -> None:
        for key in ("langgraph", "lg", "deep-agent"):
            self.assertIsNotNone(lookup(key), key)

    def test_key_check_reports_the_project_before_the_model_key(self) -> None:
        s = MagicMock(LANGGRAPH_PROJECT=None, ANTHROPIC_API_KEY=None)
        self.assertEqual(langgraph_key_check("anthropic/claude-haiku-4-5", s), "LANGGRAPH_PROJECT")


if __name__ == "__main__":
    unittest.main()
