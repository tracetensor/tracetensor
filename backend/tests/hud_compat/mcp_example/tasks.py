"""Concrete tasks for the MCP example environment."""

from env import env, use_add_tool

tasks = [
    use_add_tool(a=3, b=7),
    use_add_tool(a=12, b=25),
]

__all__ = ["env", "tasks"]
