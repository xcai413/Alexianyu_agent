"""Password and signed-session primitives for the single-admin Web console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthenticationError(ValueError):
    """A credential, session, or CSRF value cannot be trusted."""


def hash_password(password: str) -> str:
    """Return a portable scrypt hash; callers must store only this value."""
    if len(password) < 12:
        raise ValueError("管理员密码至少需要 12 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_salt, expected = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=_unb64(raw_salt), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
        )
        return hmac.compare_digest(actual, _unb64(expected))
    except (ValueError, TypeError):
        return False


def issue_session(*, signing_key: str, ttl_seconds: int) -> tuple[str, str]:
    now = int(time.time())
    csrf_token = secrets.token_urlsafe(32)
    payload = {"sub": "admin", "iat": now, "exp": now + ttl_seconds, "csrf": csrf_token}
    encoded = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.digest(signing_key.encode(), encoded.encode(), "sha256"))
    return f"{encoded}.{signature}", csrf_token


def verify_session(token: str, *, signing_key: str) -> dict[str, object]:
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64(hmac.digest(signing_key.encode(), encoded.encode(), "sha256"))
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("会话签名无效")
        payload = json.loads(_unb64(encoded))
        if payload.get("sub") != "admin" or not isinstance(payload.get("csrf"), str):
            raise AuthenticationError("会话内容无效")
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= time.time():
            raise AuthenticationError("会话已过期")
        return payload
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AuthenticationError("会话无效") from exc


def verify_csrf(session: dict[str, object], submitted: str | None) -> None:
    expected = session.get("csrf")
    if (
        not isinstance(expected, str)
        or not submitted
        or not hmac.compare_digest(expected, submitted)
    ):
        raise AuthenticationError("CSRF 校验失败")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
