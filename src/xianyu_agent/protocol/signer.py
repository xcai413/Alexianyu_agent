"""Compatibility facade for the canonical MTOP signer boundary.

New signer code should import from :mod:`xianyu_agent.protocol.mtop.signer`.
The unrelated ``make_error`` helper remains here only for legacy compatibility.
"""

from __future__ import annotations

import uuid

from xianyu_agent.protocol.events import ErrorOccurred
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    SIGN_VERSION,
    CookieSigner,
    MtopHeaders,
    compute_sign,
    derive_token_seed,
    extract_cookie_field,
    extract_mtop_token,
    make_headers,
)

__all__ = [
    "APP_KEY",
    "SIGN_VERSION",
    "CookieSigner",
    "MtopHeaders",
    "compute_sign",
    "derive_token_seed",
    "extract_cookie_field",
    "extract_mtop_token",
    "make_error",
    "make_headers",
]


def make_error(account_id: str, code: str, message: str) -> ErrorOccurred:
    """Preserve the legacy non-MTOP error helper without moving it into MTOP."""
    return ErrorOccurred(
        event_id=uuid.uuid4().hex,
        account_id=account_id,
        code=code,
        message=message,
    )
