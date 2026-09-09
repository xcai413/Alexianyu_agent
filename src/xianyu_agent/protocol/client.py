"""Compatibility facade for the canonical WebSocket client.

The implementation now lives in :mod:`xianyu_agent.protocol.ws.client`.  This
module intentionally keeps the established import surface while no longer
owning WebSocket orchestration or a receive loop.
"""

from xianyu_agent.protocol.events import ConnectionState, WsFrame
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.protocol.ws import (
    ack as ws_ack,
    connector as ws_connector,
    decoder as ws_decoder,
    heartbeat as ws_heartbeat,
    request_router as ws_request_router,
    sender as ws_sender,
)
from xianyu_agent.protocol.ws.client import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    MAX_BACKOFF_S,
    MIN_BACKOFF_S,
    AuthFailureHandler,
    ClientConfig,
    ErrorHandler,
    EventHandler,
    FrameHandler,
    StateHandler,
    WsClient,
    _default_heartbeat,
    _generate_mid,
)
from xianyu_agent.protocol.ws_auth import (
    WsAuthError,
    WsTokenProvider,
    build_registration_frame,
    build_sync_frame,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "MAX_BACKOFF_S",
    "MIN_BACKOFF_S",
    "AuthFailureHandler",
    "ClientConfig",
    "ConnectionState",
    "CookieSigner",
    "ErrorHandler",
    "EventHandler",
    "FrameHandler",
    "StateHandler",
    "WsAuthError",
    "WsClient",
    "WsFrame",
    "WsTokenProvider",
    "_default_heartbeat",
    "_generate_mid",
    "build_registration_frame",
    "build_sync_frame",
    "ws_ack",
    "ws_connector",
    "ws_decoder",
    "ws_heartbeat",
    "ws_request_router",
    "ws_sender",
]
