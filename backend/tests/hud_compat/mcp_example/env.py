"""
MCP capability example — env.py with a FastMCP tool server.

The environment starts a FastMCP server in @env.initialize that exposes
a `add_numbers` tool. The agent must call the tool and return the result.
The grader checks the result is correct.

This demonstrates TraceTensor's MCP capability: the agent receives an
mcp/2025-11-25 capability pointing at the running server.
"""

from __future__ import annotations

import asyncio

from hud import Environment
from hud.capabilities import Capability

env = Environment(name="mcp-example")

_server = None
_server_task = None
_PORT = 18040


@env.initialize
async def _start_mcp_server():
    global _server, _server_task
    try:
        from fastmcp import FastMCP
    except ImportError:
        return  # fastmcp not installed — skip capability, task still runs without it

    mcp = FastMCP("calculator")

    @mcp.tool()
    def add_numbers(a: int, b: int) -> int:
        """Add two integers together."""
        return a + b

    @mcp.tool()
    def multiply_numbers(a: int, b: int) -> int:
        """Multiply two integers together."""
        return a * b

    _server = mcp
    _server_task = asyncio.create_task(
        mcp.run_http_async(host="127.0.0.1", port=_PORT)
    )
    await asyncio.sleep(0.3)  # let it bind
    env.add_capability(
        Capability.mcp(name="calculator", url=f"http://127.0.0.1:{_PORT}/mcp")
    )


@env.shutdown
async def _stop_mcp_server():
    global _server_task
    if _server_task:
        _server_task.cancel()
        _server_task = None


@env.template(id="use_add_tool")
async def use_add_tool(a: int, b: int):
    """Agent must add two numbers. Graded by exact match."""
    expected = a + b
    answer = yield (
        f"Use the 'add_numbers' tool to add {a} and {b}. "
        f"Return only the numeric result."
    )
    try:
        got = int((answer or "").strip())
        yield 1.0 if got == expected else 0.0
    except ValueError:
        yield 0.0
