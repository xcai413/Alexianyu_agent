"""WebSocket transport connection helper."""

from __future__ import annotations

from typing import Any


def open_connection(ws_url: str, cookie_value: str) -> Any:
    """Create the existing WebSocket connection context manager."""
    try:
        import websockets  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        msg = "websockets package is required for live connections"
        raise RuntimeError(msg) from exc
    return websockets.connect(
        ws_url,
        additional_headers=[("Cookie", cookie_value)],
        ping_interval=None,
        ping_timeout=None,
    )
