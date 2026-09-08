"""WebSocket outbound text sending responsibility."""

from __future__ import annotations

from contextlib import suppress
from typing import Any


async def send_text(ws: Any | None, text: str) -> bool:
    """Send one text frame, preserving the legacy False-on-unavailable/error contract."""
    if ws is None:
        return False
    with suppress(Exception):
        await ws.send(text)
        return True
    return False
