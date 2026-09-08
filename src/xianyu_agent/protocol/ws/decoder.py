"""WebSocket wire-frame and sync-payload decoding helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import normalizer as ws_normalizer


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


def unpack_sync_payloads(
    frame: WsFrame,
    *,
    body_decoder=ws_normalizer.decode_body,
    sync_decoder=ws_normalizer.decode_sync_data,
) -> list[dict]:
    """Unpack `syncPushPackage.data[*].data` using the existing decode policy."""
    body = body_decoder(frame.body)
    if not isinstance(body, dict):
        return []
    package = body.get("syncPushPackage")
    if not isinstance(package, dict) or not isinstance(package.get("data"), list):
        return []
    payloads = []
    for entry in package["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), str):
            continue
        decoded = sync_decoder(entry["data"])
        if isinstance(decoded, dict):
            payloads.append(decoded)
    return payloads
