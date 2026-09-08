"""WebSocket wire-frame decoding helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from xianyu_agent.protocol.events import WsFrame


@dataclass(frozen=True)
class DecodedFrame:
    """One successfully decoded JSON WebSocket frame."""

    frame: WsFrame
    payload: Any
    raw_text: str


def normalize_frame_text(raw: str | bytes) -> str:
    """Normalize WebSocket text/binary input using the legacy client policy."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def decode_frame(raw: str | bytes) -> DecodedFrame | None:
    """Decode one JSON frame, returning None for non-JSON input."""
    raw_text = normalize_frame_text(raw)
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    frame = WsFrame.model_validate(payload) if isinstance(payload, dict) else WsFrame(body=raw_text)
    return DecodedFrame(frame=frame, payload=payload, raw_text=raw_text)
