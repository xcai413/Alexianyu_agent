"""WebSocket heartbeat frame construction responsibility."""

from __future__ import annotations

import json
import random
import time


def generate_mid() -> str:
    """Build the legacy Xianyu heartbeat message id."""
    random_part = int(1000 * random.random())
    timestamp = int(time.time() * 1000)
    return f"{random_part}{timestamp} 0"


def default_heartbeat() -> str:
    """Build the existing `lwp=/!` heartbeat JSON frame."""
    return json.dumps({"lwp": "/!", "headers": {"mid": generate_mid()}})
