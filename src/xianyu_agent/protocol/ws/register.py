"""WebSocket registration frame construction responsibility."""

from __future__ import annotations

import time
import uuid
from typing import Protocol


class RegistrationCredentials(Protocol):
    """Minimal credential shape required by the `/reg` wire frame."""

    access_token: str
    device_id: str


def build_registration_frame(
    credentials: RegistrationCredentials,
    *,
    app_key: str,
    user_agent: str,
) -> dict:
    """Build the existing WebSocket `/reg` registration frame."""
    return {
        "lwp": "/reg",
        "headers": {
            "cache-header": "app-key token ua wv",
            "app-key": app_key,
            "token": credentials.access_token,
            "ua": user_agent,
            "dt": "j",
            "wv": "im:3,au:3,sy:6",
            "sync": "0,0;0;0;",
            "did": credentials.device_id,
            "mid": generate_mid(),
        },
    }


def generate_mid() -> str:
    """Preserve the legacy registration message-id algorithm."""
    return f"{uuid.uuid4().int % 1000}{int(time.time() * 1000)} 0"
