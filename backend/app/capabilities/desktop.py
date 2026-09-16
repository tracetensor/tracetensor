"""
DesktopClient — VNC/RFB pixel + keyboard/mouse server.

Wraps asyncvnc to provide screenshots and HID input to an agent
driving a Linux desktop environment. Tuned for LLM agents where
the model thinks for seconds per turn — not for video streaming.

The container must expose a VNC server on the RFB port declared
in the Capability (default 5900). Our Docker base image for
desktop tasks runs x11vnc or TigerVNC bound to that port.
"""

from __future__ import annotations

import io
import logging
from contextlib import AsyncExitStack
from typing import Any, ClassVar, Literal, Self
from urllib.parse import urlsplit

from .base import Capability, CapabilityClient

log = logging.getLogger("tracetensor.capabilities.desktop")

ScreenshotFormat = Literal["png", "webp"]
_WEBP_QUALITY = 85


class DesktopClient(CapabilityClient):
    """Live VNC session. Exposes screenshot + mouse/keyboard primitives."""

    protocol: ClassVar[str] = "rfb/3.8"

    def __init__(self, cap: Capability, vnc: Any, stack: AsyncExitStack) -> None:
        self.capability = cap
        self._vnc = vnc
        self._stack = stack

    @classmethod
    async def connect(cls, cap: Capability) -> Self:
        try:
            import asyncvnc
        except ImportError as e:
            raise ImportError(
                "Desktop capability requires asyncvnc: pip install asyncvnc"
            ) from e

        parts = urlsplit(cap.url)
        host = parts.hostname or "localhost"
        port = parts.port or 5900
        password = cap.params.get("password")

        stack = AsyncExitStack()
        for attempt in range(3):
            try:
                vnc = await stack.enter_async_context(
                    asyncvnc.connect(host, port, password=password)
                )
                return cls(cap, vnc, stack)
            except Exception as exc:
                if attempt == 2:
                    raise RuntimeError(
                        f"Failed to connect to desktop at {host}:{port} "
                        f"after 3 attempts: {exc}"
                    ) from exc
                import asyncio
                await asyncio.sleep(1.0)

    async def screenshot(self, *, fmt: ScreenshotFormat = "png") -> bytes:
        """Return the current screen as PNG or WebP bytes."""
        from PIL import Image

        pixels = await self._vnc.screenshot()
        img = Image.fromarray(pixels)
        buf = io.BytesIO()
        if fmt == "png":
            img.save(buf, format="PNG")
        else:
            img.save(buf, format="WEBP", quality=_WEBP_QUALITY)
        return buf.getvalue()

    async def click(self, x: int, y: int, *, button: int = 1) -> None:
        await self._vnc.mouse.move(x, y)
        await self._vnc.mouse.click(button)

    async def double_click(self, x: int, y: int) -> None:
        await self._vnc.mouse.move(x, y)
        await self._vnc.mouse.click(1)
        await self._vnc.mouse.click(1)

    async def move(self, x: int, y: int) -> None:
        await self._vnc.mouse.move(x, y)

    async def type_text(self, text: str) -> None:
        await self._vnc.keyboard.type(text)

    async def key(self, key_name: str) -> None:
        await self._vnc.keyboard.press(key_name)

    async def close(self) -> None:
        try:
            await self._stack.aclose()
        except Exception:
            pass

    @property
    def conn(self) -> Any:
        """Raw asyncvnc client for advanced operations."""
        return self._vnc


__all__ = ["DesktopClient"]
