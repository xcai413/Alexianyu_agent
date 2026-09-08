"""WebSocket initial sync frame construction responsibility."""

from __future__ import annotations

import time
import uuid


def build_sync_frame(*, now_ms: int | None = None) -> dict:
    """Build the existing `/r/SyncStatus/ackDiff` initial sync frame."""
    timestamp_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return {
        "lwp": "/r/SyncStatus/ackDiff",
        "headers": {"mid": _generate_mid()},
        "body": [
            {
                "pipeline": "sync",
                "tooLong2Tag": "PNM,1",
                "channel": "sync",
                "topic": "sync",
                "highPts": 0,
                "pts": timestamp_ms * 1000,
                "seq": 0,
                "timestamp": timestamp_ms,
            }
        ],
    }


def _generate_mid() -> str:
    """Preserve the legacy initial-sync message-id algorithm."""
    return f"{uuid.uuid4().int % 1000}{int(time.time() * 1000)} 0"
