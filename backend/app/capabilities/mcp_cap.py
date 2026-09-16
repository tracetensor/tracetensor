"""
MCPClient — Model Context Protocol server connection.

Connects to a FastMCP or any MCP-compliant HTTP/WebSocket server,
lists its tools, and invokes them. The server address comes from the
Capability manifest (Capability.mcp(...)).

The environment starts the MCP server in an @env.initialize hook and
calls env.add_capability(Capability.mcp(url=...)) to publish its
address. This client connects to that address.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Self
from urllib.parse import urlsplit

from .base import Capability, CapabilityClient

log = logging.getLogger("tracetensor.capabilities.mcp")


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolResult:
    content: list[dict[str, Any]]
    is_error: bool = False


class MCPClient(CapabilityClient):
    """Live MCP session. Lists tools and calls them."""

    protocol: ClassVar[str] = "mcp/2025-11-25"

    def __init__(self, cap: Capability, client: Any) -> None:
        self.capability = cap
        self._client = client

    @classmethod
    async def connect(cls, cap: Capability) -> Self:
        try:
            import fastmcp
        except ImportError as e:
            raise ImportError(
                "MCP capability requires fastmcp: pip install fastmcp"
            ) from e

        url = cap.url
        transport = cap.params.get("transport", "streamable-http")
        auth_token = cap.params.get("auth_token")

        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        scheme = urlsplit(url).scheme
        if transport == "websocket" or scheme in {"ws", "wss"}:
            client = fastmcp.Client(url)
        else:
            client = fastmcp.Client(url, headers=headers or None)

        await client.__aenter__()
        return cls(cap, client)

    async def list_tools(self) -> list[Tool]:
        raw = await self._client.list_tools()
        return [
            Tool(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
            )
            for t in raw
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        result = await self._client.call_tool(name, arguments or {})
        # Normalize is_error vs isError across fastmcp versions
        is_error = getattr(result, "is_error", None) or getattr(result, "isError", False)
        content = result.content if hasattr(result, "content") else []
        if isinstance(content, list):
            normalized = [
                c if isinstance(c, dict) else
                {"type": "text", "text": str(c)}
                for c in content
            ]
        else:
            normalized = [{"type": "text", "text": str(content)}]
        return ToolResult(content=normalized, is_error=bool(is_error))

    async def close(self) -> None:
        try:
            await self._client.__aexit__(None, None, None)
        except Exception:
            pass


__all__ = ["MCPClient", "Tool", "ToolResult"]
