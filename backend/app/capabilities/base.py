"""
TraceTensor capability system.

A Capability is a named protocol endpoint the agent can connect to:
  shell   — Docker exec (always available)
  browser — Chromium over Chrome DevTools Protocol (CDP)
  desktop — VNC/RFB pixel + input server
  mcp     — Model Context Protocol server
  robot   — OpenPI/0 action-observation loop

CapabilityClient is the ABC for live connections; each protocol
sub-module implements it.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Self
from urllib.parse import urlsplit, urlunsplit

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+\-.]*):")


def _normalize_url(url: str, *, default_scheme: str, default_port: int | None) -> str:
    s = url if "://" in url else f"{default_scheme}://{url}"
    parts = urlsplit(s)
    if not parts.hostname:
        raise ValueError(f"invalid URL (no host): {url!r}")
    if parts.port is None and default_port is not None:
        userinfo = f"{parts.netloc.rpartition('@')[0]}@" if "@" in parts.netloc else ""
        hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        return urlunsplit((
            parts.scheme,
            f"{userinfo}{hostname}:{default_port}",
            parts.path, parts.query, parts.fragment,
        ))
    return s


@dataclass(frozen=True, slots=True)
class Capability:
    """Wire descriptor for one slice of environment access.

    name     — agent-facing identifier (e.g. "browser", "desktop", "tools")
    protocol — versioned protocol string (e.g. "cdp/1.3", "rfb/3.8")
    url      — where the daemon is listening
    params   — protocol-specific extras (auth tokens, display numbers, etc.)
    """

    name: str
    protocol: str
    url: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {"name": self.name, "protocol": self.protocol,
                "url": self.url, "params": dict(self.params)}

    @classmethod
    def from_manifest(cls, data: dict[str, Any]) -> Capability:
        return cls(
            name=data["name"],
            protocol=data["protocol"],
            url=data["url"],
            params=dict(data.get("params") or {}),
        )

    # ── protocol factories ────────────────────────────────────────────

    @classmethod
    def shell(cls, *, name: str = "shell") -> Capability:
        """Docker exec shell — always available, no URL needed."""
        return cls(name=name, protocol="shell/exec", url="", params={})

    @classmethod
    def browser(
        cls,
        *,
        name: str = "browser",
        url: str,
        target_id: str | None = None,
    ) -> Capability:
        """Chrome DevTools Protocol (CDP) over WebSocket."""
        normalized = _normalize_url(url, default_scheme="ws", default_port=9222)
        params: dict[str, Any] = {}
        if target_id:
            params["target_id"] = target_id
        return cls(name=name, protocol="cdp/1.3", url=normalized, params=params)

    @classmethod
    def desktop(
        cls,
        *,
        name: str = "desktop",
        url: str,
        password: str | None = None,
        display: int = 0,
    ) -> Capability:
        """VNC/RFB pixel + keyboard/mouse server."""
        normalized = _normalize_url(url, default_scheme="rfb", default_port=5900 + display)
        params: dict[str, Any] = {"display": display}
        if password:
            params["password"] = password
        return cls(name=name, protocol="rfb/3.8", url=normalized, params=params)

    @classmethod
    def mcp(
        cls,
        *,
        name: str = "tools",
        url: str,
        auth_token: str | None = None,
        transport: Literal["sse", "streamable-http", "websocket"] | None = None,
    ) -> Capability:
        """Model Context Protocol server (HTTP or WebSocket transport)."""
        normalized = _normalize_url(url, default_scheme="http", default_port=None)
        scheme = urlsplit(normalized).scheme
        if scheme not in {"ws", "wss", "http", "https"}:
            raise ValueError(f"mcp: unsupported scheme {scheme!r}")
        if transport is None:
            transport = "websocket" if scheme in {"ws", "wss"} else "streamable-http"
        params: dict[str, Any] = {"transport": transport}
        if auth_token:
            params["auth_token"] = auth_token
        return cls(name=name, protocol="mcp/2025-11-25", url=normalized, params=params)

    @classmethod
    def robot(
        cls,
        *,
        name: str = "robot",
        url: str,
        contract: dict[str, Any],
    ) -> Capability:
        """OpenPI/0 action-observation loop over WebSocket."""
        normalized = _normalize_url(url, default_scheme="ws", default_port=9091)
        return cls(name=name, protocol="openpi/0", url=normalized,
                   params={"contract": contract})


class CapabilityClient(ABC):
    """Live connection to a Capability. Each protocol module subclasses this."""

    protocol: ClassVar[str]

    @classmethod
    @abstractmethod
    async def connect(cls, cap: Capability) -> Self: ...

    @abstractmethod
    async def close(self) -> None: ...


__all__ = ["Capability", "CapabilityClient"]
