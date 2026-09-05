"""In-sandbox payload for the LangGraph adapter — NOT imported by the backend.

This file is shipped into the container by `langgraph_agent.py` and run by the
container's own Python, where `langgraph` is installed. It is deliberately
dependency-free at module scope (stdlib only until the graph is built) so that a
missing dependency surfaces as our own JSON error record rather than a bare
traceback the trial runner can't attribute.

Contract with the adapter:
  in   TT_LANGGRAPH_PROJECT  project dir holding langgraph.json  (default /langgraph)
       TT_LANGGRAPH_GRAPH    which graphs{} entry to run         (default: first)
       TT_INSTRUCTION_FILE   file holding instruction.md's text
       TT_WORKDIR            cwd for the graph, so relative writes land in the task
       TT_RECURSION_LIMIT    LangGraph recursion limit
  out  one JSON object on stdout between the two markers below

The markers exist because agent frameworks print freely to stdout (LangChain
emits warnings, tqdm bars, telemetry notices). Delimiting our record means the
adapter parses the payload it wrote rather than guessing which line is JSON.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import pathlib
import sys
import traceback

BEGIN = "---TT-LANGGRAPH-RESULT-BEGIN---"
END = "---TT-LANGGRAPH-RESULT-END---"


def _emit(payload: dict) -> None:
    sys.stdout.flush()
    print(BEGIN)
    print(json.dumps(payload, default=str))
    print(END)
    sys.stdout.flush()


def _load_registry(project: pathlib.Path) -> dict:
    registry = project / "langgraph.json"
    if not registry.exists():
        raise RuntimeError(
            f"No langgraph.json in {project} — a LangGraph project must declare its "
            "graphs in a langgraph.json registry."
        )
    return json.loads(registry.read_text(encoding="utf-8"))


def _resolve_ref(cfg: dict, wanted: str | None) -> tuple[str, str]:
    """(graph_name, 'module_or_path:attr') for the requested graph."""
    graphs = cfg.get("graphs") or {}
    if not graphs:
        raise RuntimeError("langgraph.json declares no 'graphs' entries.")
    if wanted:
        if wanted not in graphs:
            known = ", ".join(sorted(graphs))
            raise RuntimeError(f"Graph {wanted!r} not in langgraph.json. Declared: {known}.")
        return wanted, graphs[wanted]
    name = next(iter(graphs))
    return name, graphs[name]


def _import_target(project: pathlib.Path, ref: str):
    """Import the module half of 'X:attr' — a ./file.py path or a dotted module."""
    if ":" not in ref:
        raise RuntimeError(
            f"Graph reference {ref!r} must be '<module-or-file>:<attribute>', "
            "e.g. './agent.py:make_agent'."
        )
    target, attr = ref.rsplit(":", 1)
    if target.endswith(".py"):
        path = (project / target).resolve()
        if not path.exists():
            raise RuntimeError(f"Graph file {path} does not exist (from ref {ref!r}).")
        spec = importlib.util.spec_from_file_location("tt_langgraph_target", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {path} as a Python module.")
        module = importlib.util.module_from_spec(spec)
        sys.modules["tt_langgraph_target"] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    if not hasattr(module, attr):
        raise RuntimeError(f"{target} has no attribute {attr!r} (from ref {ref!r}).")
    return getattr(module, attr)


def _is_invokable(obj) -> bool:
    return hasattr(obj, "invoke") or hasattr(obj, "ainvoke")


def _compile_if_needed(graph):
    """A ref may point at an uncompiled builder; compile it before invoking."""
    if _is_invokable(graph):
        return graph
    if hasattr(graph, "compile"):
        return graph.compile()
    raise RuntimeError(
        f"Graph reference produced {type(graph).__name__}, which has neither "
        "`invoke`/`ainvoke` nor `compile`."
    )


def _call_factory(factory, config: dict):
    """Call a graph factory, passing `config` only if it accepts an argument.

    LangGraph's deployment convention is `def make_graph(config)`, where the
    factory reads `config["configurable"]` to pick its model at runtime rather
    than baking one in at import. Calling every factory with no arguments broke
    any project written that way, so the arity decides the call.

    An unreadable signature (C callables, exotic decorators) is assumed to want
    the config, since that is the documented convention — a no-arg factory that
    hides its signature is the rarer accident and fails with a clear TypeError.
    """
    try:
        takes_config = len(inspect.signature(factory).parameters) >= 1
    except (TypeError, ValueError):
        takes_config = True
    return factory(config) if takes_config else factory()


async def _invoke(graph, payload: dict, config: dict):
    """Prefer the async entrypoint — a graph whose tools are async only
    implements `ainvoke`, and calling `invoke` on it deadlocks or raises."""
    if hasattr(graph, "ainvoke"):
        return await graph.ainvoke(payload, config=config)
    return graph.invoke(payload, config=config)


async def _resolve_and_invoke(target, payload: dict, config: dict):
    """Turn whatever the registry ref pointed at into a result.

    Resolution and invocation live together because a factory is allowed to
    return a context manager — a graph holding a connection pool or an MCP
    session must stay open across the invoke, so the scope cannot be exited
    before the graph runs.
    """
    produced = target if _is_invokable(target) else target
    if not _is_invokable(target):
        if not callable(target):
            raise RuntimeError(
                f"Graph reference produced {type(target).__name__}, which is neither "
                "an invokable graph nor a factory that builds one."
            )
        produced = _call_factory(target, config)

    if inspect.isawaitable(produced):  # async def make_graph(...)
        produced = await produced

    if hasattr(produced, "__aenter__"):
        async with produced as graph:
            return await _invoke(_compile_if_needed(graph), payload, config)
    if hasattr(produced, "__enter__") and not _is_invokable(produced):
        with produced as graph:
            return await _invoke(_compile_if_needed(graph), payload, config)
    return await _invoke(_compile_if_needed(produced), payload, config)


def _usage(messages: list) -> dict:
    """Sum LangChain's per-message usage_metadata into one run-scoped record.

    Only AI messages carry usage. Summing them gives run totals — the same
    choice claude_code.py makes with `modelUsage` over per-turn `usage`.
    """
    calls = 0
    inp = out = 0
    model = None
    for m in messages:
        meta = getattr(m, "usage_metadata", None)
        if not isinstance(meta, dict):
            continue
        calls += 1
        inp += int(meta.get("input_tokens") or 0)
        out += int(meta.get("output_tokens") or 0)
        if model is None:
            rmeta = getattr(m, "response_metadata", None) or {}
            model = rmeta.get("model_name") or rmeta.get("model")
    return {"api_calls": calls, "input_tokens": inp, "output_tokens": out, "model": model}


def _transcript(messages: list, limit: int = 60) -> list:
    """A compact, JSON-safe view of the run for the trajectory record."""
    out = []
    for m in messages[:limit]:
        entry = {"type": m.__class__.__name__}
        content = getattr(m, "content", None)
        if isinstance(content, str):
            entry["content"] = content[:2000]
        elif content is not None:
            entry["content"] = str(content)[:2000]
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [
                {"name": tc.get("name"), "args": str(tc.get("args"))[:500]}
                for tc in tool_calls
                if isinstance(tc, dict)
            ]
        out.append(entry)
    return out


def main() -> int:
    project = pathlib.Path(os.environ.get("TT_LANGGRAPH_PROJECT", "/langgraph"))
    workdir = os.environ.get("TT_WORKDIR") or "/app"
    instruction_file = os.environ.get("TT_INSTRUCTION_FILE", "/langgraph/instruction.txt")
    limit = int(os.environ.get("TT_RECURSION_LIMIT", "50"))

    # Passed to the factory and to invoke. `configurable` is where LangGraph
    # expects runtime settings, so a factory written to the deployment
    # convention picks up the run's model from here instead of hardcoding one.
    config: dict = {"recursion_limit": limit, "configurable": {}}
    if os.environ.get("TT_MODEL"):
        config["configurable"]["model"] = os.environ["TT_MODEL"]

    try:
        cfg = _load_registry(project)
        graph_name, ref = _resolve_ref(cfg, os.environ.get("TT_LANGGRAPH_GRAPH"))
        # The project dir goes on sys.path so a graph module can import its own
        # siblings ("from tools import ...") the way it would when run locally.
        sys.path.insert(0, str(project))
        target = _import_target(project, ref)
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "phase": "build",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-4000:],
            }
        )
        return 1

    instruction = pathlib.Path(instruction_file).read_text(encoding="utf-8")

    # The graph works on the task's files, so it must run where the verifier will
    # look. A relative write in a tool ("reports/x.md") is otherwise resolved
    # against the project dir and the task scores 0 for a file that does exist.
    os.chdir(workdir)

    # Building and invoking share one scope: a factory may hand back a context
    # manager that has to stay open while the graph runs.
    try:
        result = asyncio.run(
            _resolve_and_invoke(
                target,
                {"messages": [{"role": "user", "content": instruction}]},
                config,
            )
        )
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "phase": "invoke",
                "graph": graph_name,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-4000:],
            }
        )
        return 1

    messages = result.get("messages", []) if isinstance(result, dict) else []
    _emit(
        {
            "ok": True,
            "graph": graph_name,
            "ref": ref,
            "usage": _usage(messages),
            "messages": _transcript(messages),
            "final": getattr(messages[-1], "content", None) if messages else None,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
