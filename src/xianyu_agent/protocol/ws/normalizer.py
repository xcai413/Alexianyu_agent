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


def _canonicalize_sync_keys(value):
    """Recursively canonicalize integer protocol map keys to strings."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            canonical_key = (
                str(key)
                if isinstance(key, int) and not isinstance(key, bool)
                else key
            )
            if canonical_key in result:
                raise ValueError(
                    f"sync map key collision after canonicalization: {canonical_key!r}"
                )
            result[canonical_key] = _canonicalize_sync_keys(item)
        return result

    if isinstance(value, list):
        return [_canonicalize_sync_keys(item) for item in value]

    return value


def decode_sync_data(value: str) -> dict | None:  # noqa: PLR0911
    """Decode one Base64 sync payload as JSON first, then MessagePack."""
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None

    try:
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            return None
        return _canonicalize_sync_keys(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        pass

    try:
        decoded = msgpack.unpackb(raw, raw=False, strict_map_key=False)
    except (
        ValueError,
        TypeError,
        msgpack.ExtraData,
        msgpack.FormatError,
        msgpack.StackError,
    ):
        return None

    if not isinstance(decoded, dict):
        return None

    try:
        return _canonicalize_sync_keys(decoded)
    except ValueError:
        return None
