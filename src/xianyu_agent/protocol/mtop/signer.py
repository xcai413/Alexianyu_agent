"""MTOP token signing and encrypted Cookie persistence.

This module owns the existing MTOP signing wire contract and Cookie persistence behavior.
The implementation is mechanically migrated from ``xianyu_agent.protocol.signer``; the
legacy module remains a compatibility facade.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, Cookie, WsCredential, get_async_session
from xianyu_agent.protocol.events import ErrorOccurred

logger = logging.getLogger(__name__)
APP_KEY = "34839810"
SIGN_VERSION = "1.0"


@dataclass(frozen=True)
class MtopHeaders:
    """The minimum headers required to call a mtop endpoint."""

    app_key: str
    timestamp_ms: int
    sign: str
    token: str

    @property
    def x_t(self) -> str:
        return str(self.timestamp_ms)

    @property
    def x_sign(self) -> str:
        return self.sign


def compute_sign(token_seed: str, timestamp_ms: int, app_key: str, data: str) -> str:
    """mtop sign = md5(f"{token_seed}&{ts}&{app_key}&{data}")."""
    raw = f"{token_seed}&{timestamp_ms}&{app_key}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def derive_token_seed(m_h5_tk: str) -> str:
    """The seed is the substring before the first underscore."""
    if not m_h5_tk:
        msg = "_m_h5_tk is empty"
        raise ValueError(msg)
    return m_h5_tk.split("_", 1)[0]


def extract_mtop_token(cookie: str) -> str | None:
    """Extract the mtop token from either observed cookie spelling.

    The platform has used both ``_m_h5_tk`` and ``m_h5_tk`` in different
    flows. Callers receive only the value in memory and must never log it.
    """
    return extract_cookie_field(cookie, "_m_h5_tk") or extract_cookie_field(
        cookie, "m_h5_tk"
    )


def extract_cookie_field(cookie: str, name: str, *, prefix_only: bool = False) -> str | None:
    """从 Cookie header 中读取一个字段;调用方不得记录返回的敏感值。"""
    return _extract_field(cookie, name, prefix_only=prefix_only)


def make_headers(m_h5_tk: str, *, data: str = "") -> MtopHeaders:
    """Build a fresh MtopHeaders bundle. Timestamp is now (ms)."""
    seed = derive_token_seed(m_h5_tk)
    ts_ms = int(time.time() * 1000)
    sign = compute_sign(seed, ts_ms, APP_KEY, data)
    return MtopHeaders(
        app_key=APP_KEY,
        timestamp_ms=ts_ms,
        sign=sign,
        token=m_h5_tk,
    )


class CookieSigner:
    """Read/write encrypted cookies and produce mtop headers on demand.

    Holds no long-lived state: every call decrypts the latest row.
    """

    def __init__(self, fernet=None):
        self._fernet = fernet

    @property
    def fernet(self):
        """Lazily resolve Fernet so construction works in offline contexts."""
        if self._fernet is None:
            self._fernet = get_settings().fernet
        return self._fernet

    async def load_cookie_value(self, account_id: str) -> str | None:
        """Return the decrypted full cookie string for an account, or None."""
        async with get_async_session() as session:
            stmt = (
                select(Cookie)
                .join(Account, Account.id == Cookie.account_id)
                .where(Account.account_id == account_id)
                .order_by(Cookie.updated_at.desc(), Cookie.id.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            try:
                return self.fernet.decrypt(row.encrypted_value.encode("utf-8")).decode(
                    "utf-8"
                )
            except Exception as exc:
                logger.warning("decrypt failed for account=%s: %s", account_id, exc)
                return None

    async def save_cookie(self, account_id: str, cookie_value: str) -> bool:
        """Persist a fresh cookie and invalidate the old IM token.

        The device ID is deliberately retained so a QR refresh does not create a
        new device fingerprint. The next WS start must exchange the new Cookie
        for a fresh accessToken.
        """
        async with get_async_session() as session:
            stmt = select(Account).where(Account.account_id == account_id).limit(1)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if account is None:
                return False
            encrypted = self.fernet.encrypt(cookie_value.encode("utf-8")).decode("utf-8")
            session.add(
                Cookie(
                    account_id=account.id,
                    encrypted_value=encrypted,
                )
            )
            credential = (
                await session.execute(
                    select(WsCredential)
                    .where(WsCredential.account_id == account.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if credential is not None:
                credential.encrypted_token = self.fernet.encrypt(b"").decode("utf-8")
                credential.expires_at = datetime.now(UTC)
            account.last_login_at = datetime.now(UTC)
            await session.commit()
            return True

    async def make_headers(self, account_id: str, *, data: str = "") -> MtopHeaders | None:
        """Build mtop headers for an account, or None if no cookie is stored."""
        cookie = await self.load_cookie_value(account_id)
        if not cookie:
            return None
        m_h5_tk = extract_mtop_token(cookie)
        if not m_h5_tk:
            return None
        return make_headers(m_h5_tk, data=data)

    async def load_user_id(self, account_id: str) -> str | None:
        """Load the upstream user ID for protocol calls; callers must not log it."""
        cookie = await self.load_cookie_value(account_id)
        if not cookie:
            return None
        return _extract_field(cookie, "unb") or _extract_field(cookie, "munb")

    async def fingerprint(self, account_id: str) -> dict[str, Any] | None:
        """Return a safe-to-print summary of the cookie for diagnostics."""
        cookie = await self.load_cookie_value(account_id)
        if not cookie:
            return None
        return {
            "account_id": account_id,
            "has_unb": bool(_extract_field(cookie, "unb") or _extract_field(cookie, "munb")),
            "unb_masked": _mask_identifier(
                _extract_field(cookie, "unb") or _extract_field(cookie, "munb")
            ),
            "has_m_h5_tk": bool(extract_mtop_token(cookie)),
            "has_cookie2": bool(_extract_field(cookie, "cookie2")),
            "m_h5_tk_digest": _secret_digest(extract_mtop_token(cookie)),
            "loaded_at": datetime.now(UTC).isoformat(),
        }


def _extract_field(cookie: str, name: str, *, prefix_only: bool = False) -> str | None:
    """Extract a single field from a Netscape cookie string."""
    for part in cookie.split(";"):
        part = part.strip()  # noqa: PLW2901
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key == name:
            if prefix_only and len(value) > 12:
                return value[:12] + "..."
            return value
    return None


def _mask_identifier(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{value[:2]}***{value[-2:]}#{digest}"


def _secret_digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None


def make_error(account_id: str, code: str, message: str) -> ErrorOccurred:
    return ErrorOccurred(
        event_id=uuid.uuid4().hex,
        account_id=account_id,
        code=code,
        message=message,
    )
