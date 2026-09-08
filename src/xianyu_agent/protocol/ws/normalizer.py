"""WebSocket body and sync-payload normalization helpers."""

from __future__ import annotations

import base64
import json

import msgpack


def decode_body(body):
    """Normalize a frame body into a mapping when it is JSON or Base64 JSON."""
    if body is None:
        return None
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        return None
    try:
        result = json.loads(body)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        decoded = base64.b64decode(body, validate=True)
        result = json.loads(decoded)
        if isinstance(result, dict):
            return result
    except (ValueError, json.JSONDecodeError):
        pass
    return None


def decode_sync_data(value: str) -> dict | None:
    """Decode one Base64 sync payload as JSON first, then MessagePack."""
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    try:
        decoded = msgpack.unpackb(raw, raw=False, strict_map_key=False)
    except (ValueError, TypeError, msgpack.ExtraData, msgpack.FormatError, msgpack.StackError):
        return None
    return decoded if isinstance(decoded, dict) else None
