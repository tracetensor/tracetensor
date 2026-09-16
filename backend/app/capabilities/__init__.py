"""TraceTensor capability system — Browser, Desktop, MCP, Robot."""

from .base import Capability, CapabilityClient
from .browser import BrowserClient
from .desktop import DesktopClient
from .mcp_cap import MCPClient, Tool, ToolResult

__all__ = [
    "Capability",
    "CapabilityClient",
    "BrowserClient",
    "DesktopClient",
    "MCPClient",
    "Tool",
    "ToolResult",
]
