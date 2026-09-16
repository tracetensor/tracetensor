"""
BrowserClient — Chrome DevTools Protocol over a single page-target WebSocket.

Opens one WebSocket to a Chromium page target and speaks CDP JSON-RPC.
A background reader demuxes command replies from protocol events.
Public verb: send(method, params) → response dict.

Chromium must be running with --remote-debugging-port=9222 (or whatever
port the Capability.browser() URL declares). Our Docker base image for
browser tasks starts Chromium with that flag.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, ClassVar, Self
from urllib.parse import urlsplit

from .base import Capability, CapabilityClient

log = logging.getLogger("tracetensor.capabilities.browser")


class BrowserError(RuntimeError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        super().__init__(f"CDP {method!r} [{error.get('code')}]: {error.get('message', '')}")
        self.code = error.get("code")
        self.message = error.get("message", "")


class BrowserClient(CapabilityClient):
    """Live CDP session bound to one Chromium page target."""

    protocol: ClassVar[str] = "cdp/1.3"

    def __init__(self, cap: Capability, ws: Any) -> None:
        self.capability = cap
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, cap: Capability) -> Self:
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError as e:
            raise ImportError(
                "Browser capability requires websockets: pip install websockets"
            ) from e

        parts = urlsplit(cap.url)
        if not parts.hostname or not parts.port:
            raise ValueError(f"browser capability missing host or port: {cap.url!r}")

        ws_url = await cls._resolve_page_ws(parts.hostname, parts.port,
                                             cap.params.get("target_id"), cap.url)
        ws = await ws_connect(ws_url, max_size=None)
        client = cls(cap, ws)
        client._reader = asyncio.create_task(client._read_loop())
        await client.send("Page.enable")
        await client.send("Runtime.enable")
        await client.send("DOM.enable")
        return client

    @classmethod
    async def _resolve_page_ws(
        cls, host: str, port: int, target_id: str | None, raw_url: str
    ) -> str:
        if target_id:
            return f"ws://{host}:{port}/devtools/page/{target_id}"
        if "/devtools/" in raw_url:
            return raw_url
        try:
            import httpx
            async with httpx.AsyncClient() as c:
                # Fallback 2: GET /json — list existing page targets
                resp = await c.get(f"http://{host}:{port}/json", timeout=5.0)
                targets = resp.json()
                pages = [t for t in targets if t.get("type") == "page"]

                # Fallback 3: PUT /json/new — create a fresh page if none exist
                if not pages:
                    resp2 = await c.put(f"http://{host}:{port}/json/new", timeout=5.0)
                    new_target = resp2.json()
                    if new_target and new_target.get("webSocketDebuggerUrl"):
                        pages = [new_target]
                        log.info("browser_new_page_created",
                                 extra={"target_id": new_target.get("id")})

            ws_url = pages[0].get("webSocketDebuggerUrl", "") if pages else ""
            if ws_url:
                return ws_url
        except Exception as exc:
            log.warning("browser_target_discovery_failed", extra={"error": str(exc)})
        return f"ws://{host}:{port}"

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
        except Exception as exc:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            self._pending.clear()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg_id = next(self._ids)
        payload = {"id": msg_id, "method": method, "params": params or {}}
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps(payload))
        result = await asyncio.wait_for(fut, timeout=30.0)
        if "error" in result:
            raise BrowserError(method, result["error"])
        return result.get("result", {})

    async def screenshot(self, *, fmt: str = "png") -> bytes:
        """Return a raw screenshot as PNG or JPEG bytes."""
        import base64
        result = await self.send("Page.captureScreenshot", {"format": fmt})
        return base64.b64decode(result["data"])

    async def navigate(self, url: str, *, wait_for: str = "load") -> None:
        await self.send("Page.navigate", {"url": url})
        await self.send("Page.waitForNavigation",
                        {"waitUntil": wait_for} if wait_for != "load" else {})

    async def evaluate(self, expression: str) -> Any:
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        return result.get("result", {}).get("value")

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass


__all__ = ["BrowserClient", "BrowserError"]
