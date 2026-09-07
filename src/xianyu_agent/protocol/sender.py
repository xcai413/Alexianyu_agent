"""Outbound WebSocket text sender boundary."""

from __future__ import annotations

from contextlib import suppress
from typing import Any


class SocketSender:
    """Best-effort text sender preserving the legacy boolean contract."""

    async def send_text(self, socket: Any | None, text: str) -> bool:
        if socket is None:
            return False
        with suppress(Exception):
            await socket.send(text)
            return True
        return False
