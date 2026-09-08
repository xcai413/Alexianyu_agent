"""Compatibility facade for the canonical MTOP signer boundary.

New code should import from :mod:`xianyu_agent.protocol.mtop.signer`.
"""

from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    SIGN_VERSION,
    CookieSigner,
    MtopHeaders,
    compute_sign,
    derive_token_seed,
    extract_cookie_field,
    extract_mtop_token,
    make_error,
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
