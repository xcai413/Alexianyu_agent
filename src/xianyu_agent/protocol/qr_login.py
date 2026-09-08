"""Compatibility facade for the canonical QR login protocol adapter.

New code should import from :mod:`xianyu_agent.protocol.auth.qr`.  This module keeps
legacy imports working while the protocol package is migrated incrementally.
"""

from xianyu_agent.protocol.auth.qr import (
    API_GENERATE_QR,
    API_MINI_LOGIN,
    API_SCAN_STATUS,
    APP_KEY,
    BROWSER_HEADERS,
    H5API_INDEX,
    HTTP_RETRY_DELAYS_S,
    PASSPORT_HOST,
    POLL_INTERVAL_S,
    QRLoginClient,
    QRLoginSession,
    QrLoginError,
    QrStatus,
    SESSION_TTL_S,
)

__all__ = [
    "API_GENERATE_QR",
    "API_MINI_LOGIN",
    "API_SCAN_STATUS",
    "APP_KEY",
    "BROWSER_HEADERS",
    "H5API_INDEX",
    "HTTP_RETRY_DELAYS_S",
    "PASSPORT_HOST",
    "POLL_INTERVAL_S",
    "QRLoginClient",
    "QRLoginSession",
    "QrLoginError",
    "QrStatus",
    "SESSION_TTL_S",
]
