"""Single-admin browser authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from xianyu_agent.application.web_sessions import WebSessionService
from xianyu_agent.config import Settings, get_settings

from .security import (
    AuthenticationError,
    issue_session,
    verify_csrf,
    verify_password,
    verify_session,
)

router = APIRouter(prefix="/api/v1/web/auth", tags=["web-auth"])
SESSION_COOKIE = "xianyu_web_session"
CSRF_HEADER = "X-CSRF-Token"


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


def _settings() -> Settings:
    settings = get_settings()
    if not settings.web_session_secret or not settings.web_admin_password_hash:
        raise HTTPException(status_code=503, detail={"code": "WEB_AUTH_UNCONFIGURED"})
    return settings


async def require_admin(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, object]:
    """FastAPI dependency for every Web-only business endpoint."""
    settings = _settings()
    if not session_token:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    try:
        session = verify_session(session_token, signing_key=settings.web_session_secret)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"}) from exc
    try:
        revoked = await WebSessionService.is_revoked(session_token)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "WEB_SESSION_STORAGE_UNAVAILABLE"}
        ) from exc
    if revoked:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED"})
    return session


async def require_admin_csrf(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, object]:
    """Require an authenticated browser session and its matching CSRF token."""
    session = await require_admin(session_token)
    try:
        verify_csrf(session, csrf_token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=403, detail={"code": "CSRF_INVALID"}) from exc
    return session


@router.post("/login")
async def login(body: LoginRequest, response: Response) -> dict[str, object]:
    settings = _settings()
    if not settings.web_admin_password_hash or not verify_password(
        body.password, settings.web_admin_password_hash
    ):
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID_CREDENTIALS"})
    token, csrf_token = issue_session(
        signing_key=settings.web_session_secret, ttl_seconds=settings.web_session_ttl_seconds
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.web_cookie_secure,
        samesite="strict",
        max_age=settings.web_session_ttl_seconds,
        path="/",
    )
    return {"data": {"authenticated": True, "csrf_token": csrf_token}}


@router.get("/session")
async def session(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, object]:
    _settings()
    if not session_token:
        return {"data": {"authenticated": False}}
    try:
        await require_admin(session_token)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        return {"data": {"authenticated": False}}
    return {"data": {"authenticated": True}}


@router.post("/logout")
async def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> dict[str, object]:
    session = await require_admin_csrf(session_token, csrf_token)
    assert session_token is not None
    expires_at = session["exp"]
    assert isinstance(expires_at, int)
    try:
        await WebSessionService.revoke(session_token, expires_at=expires_at)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "WEB_SESSION_STORAGE_UNAVAILABLE"}
        ) from exc
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"data": {"authenticated": False}}
