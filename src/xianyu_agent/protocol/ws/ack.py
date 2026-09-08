"""WebSocket ACK construction and sending responsibility."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from xianyu_agent.protocol.events import WsFrame


def build_ack_frame(
    frame: WsFrame,
    *,
    mid_factory: Callable[[], str] | None = None,
) -> dict | None:
    """Build the existing ACK frame for a server push when correlation exists."""
    headers = frame.headers if isinstance(frame.headers, dict) else {}
    if not headers:
        return None
    mid = headers.get("mid")
    sid = headers.get("sid")
    if not mid and not sid:
        return None
    ack_headers = {
        "mid": str(mid or (mid_factory or fallback_mid)()),
        "sid": str(sid or ""),
    }
    for key in ("app-key", "ua", "dt"):
        if key in headers:
            ack_headers[key] = headers[key]
    return {"code": 200, "headers": ack_headers}


async def send_ack(ws: Any | None, frame: WsFrame) -> bool:
    """Send the existing protocol ACK when one is required and a socket exists."""
    ack = build_ack_frame(frame)
    if ack is None or ws is None:
        return False
    await ws.send(json.dumps(ack))
    return True


def fallback_mid() -> str:
    """Preserve the legacy ACK fallback message-id algorithm."""
    return f"{uuid.uuid4().int % 1000}{int(datetime.now(UTC).timestamp() * 1000)} 0"
