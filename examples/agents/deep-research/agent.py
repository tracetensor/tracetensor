"""A small deep-research agent: read the sources, synthesise, write the report.

This is the *candidate under test* — the equivalent of the Deep Agent in
LangChain's Harbor demo. It is deliberately ordinary LangGraph: a ReAct loop
with three filesystem tools, imported from a sibling `tools/` package the way a
real project would lay them out. TraceTensor never imports this file; the runner
inside the sandbox resolves it from langgraph.json and calls `make_agent()`.

The model id arrives as TT_MODEL so `tracetensor run -m …` reaches the graph.
"""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

from tools import list_files, read_file, write_file

SYSTEM_PROMPT = """You are a research assistant working inside a sandbox.

Work in this order, and do not skip a step:
1. Call list_files on the directory the task names, to see what sources exist.
2. Call read_file on the source that matches the requested subject.
3. Write the report with write_file, following the required format exactly.

Ground every claim in the source you read. Do not invent figures. When the task
asks you to cite sources, cite the source's filename.
Finish as soon as the report is written — do not re-read it."""


def make_agent(config):
    """Build the compiled graph. Referenced by langgraph.json.

    Takes a required `config` per LangGraph's deployment convention, so the model
    is chosen at run time from `config["configurable"]` rather than baked in at
    import. Required rather than defaulted on purpose: a harness that calls
    factories with no arguments fails loudly here instead of silently ignoring
    the run's model. Call `make_agent({})` to build it by hand.
    """
    configurable = (config or {}).get("configurable") or {}
    model_id = configurable.get("model") or os.environ.get("TT_MODEL") or "claude-haiku-4-5"
    model = ChatAnthropic(model=model_id, max_tokens=4096, temperature=0)
    return create_react_agent(model, [list_files, read_file, write_file], prompt=SYSTEM_PROMPT)
