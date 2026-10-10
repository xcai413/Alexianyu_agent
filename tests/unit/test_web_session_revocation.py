"""Session copies must stay revoked across requests, restarts and shared instances."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from time import time

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from xianyu_agent.application import web_sessions
from xianyu_agent.application.web_sessions import WebSessionService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.infrastructure.database.models import RevokedWebSession
from xianyu_agent.interfaces.web.auth import security
from xianyu_agent.interfaces.web.auth.router import router
from xianyu_agent.interfaces.web.auth.security import hash_password, verify_session
from xianyu_agent.interfaces.web.dashboard.router import router as dashboard_router

PASSWORD = "isolated administrator password"
SIGNING_KEY = "isolated-session-signing-key"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(dashboard_router)
    return app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANYU_DATABASE_URL", "")
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("XIANYU_WEB_ADMIN_PASSWORD_HASH", hash_password(PASSWORD))
    monkeypatch.setenv("XIANYU_WEB_SESSION_SECRET", SIGNING_KEY)
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        yield client
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _login(client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/api/v1/web/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200
    token = client.cookies.get("xianyu_web_session")
    assert token is not None
    return token, {
        "Cookie": f"xianyu_web_session={token}",
        "X-CSRF-Token": response.json()["data"]["csrf_token"],
    }


async def test_logout_revocation_survives_restart_and_new_app(client):
    token, headers = await _login(client)
    assert (await client.post("/api/v1/web/auth/logout", headers=headers)).status_code == 200
    await db_mod.async_engine.dispose()
    db_mod.reset_engine()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as other:
        assert (await other.get("/api/v1/dashboard/summary", headers=headers)).status_code == 401
        assert (await other.get("/api/v1/web/auth/session", headers=headers)).json() == {
            "data": {"authenticated": False}
        }
        await _login(other)
        assert (await other.get("/api/v1/dashboard/summary")).status_code == 200
    async with db_mod.get_async_session() as session:
        rows = list((await session.execute(select(RevokedWebSession))).scalars())
    assert len(rows) == 1
    assert rows[0].session_hash == sha256(token.encode()).hexdigest()
    assert rows[0].expires_at == verify_session(token, signing_key=SIGNING_KEY)["exp"]
    assert token not in rows[0].session_hash


async def test_logout_of_one_login_preserves_another_active_login(client):
    first, first_headers = await _login(client)
    second, second_headers = await _login(client)
    assert first != second
    assert (await client.post("/api/v1/web/auth/logout", headers=first_headers)).status_code == 200
    assert (await client.get("/api/v1/dashboard/summary", headers=first_headers)).status_code == 401
    assert (
        await client.get("/api/v1/dashboard/summary", headers=second_headers)
    ).status_code == 200


async def test_bad_csrf_does_not_revoke_or_delete_valid_session(client):
    _, headers = await _login(client)
    rejected = await client.post(
        "/api/v1/web/auth/logout", headers={**headers, "X-CSRF-Token": "wrong"}
    )
    assert rejected.status_code == 403
    assert "set-cookie" not in rejected.headers
    assert (await client.get("/api/v1/dashboard/summary", headers=headers)).status_code == 200
    async with db_mod.get_async_session() as session:
        assert list((await session.execute(select(RevokedWebSession))).scalars()) == []


async def test_concurrent_copied_logout_commits_one_revocation(client):
    token, headers = await _login(client)
    responses = await asyncio.gather(
        *(client.post("/api/v1/web/auth/logout", headers=headers) for _ in range(3))
    )
    assert 200 in [r.status_code for r in responses]
    assert all(r.status_code in {200, 401} for r in responses)
    assert (await client.get("/api/v1/dashboard/summary", headers=headers)).status_code == 401
    # Exercise the duplicate commit path independently of request-time rejection.
    expires_at = verify_session(token, signing_key=SIGNING_KEY)["exp"]
    assert isinstance(expires_at, int)
    await asyncio.gather(
        *(WebSessionService.revoke(token, expires_at=expires_at) for _ in range(3))
    )
    async with db_mod.get_async_session() as session:
        assert len(list((await session.execute(select(RevokedWebSession))).scalars())) == 1


async def test_expired_records_do_not_remove_live_revocations(client):
    _, first_headers = await _login(client)
    assert (await client.post("/api/v1/web/auth/logout", headers=first_headers)).status_code == 200
    async with db_mod.get_async_session() as session:
        session.add(RevokedWebSession(session_hash="0" * 64, expires_at=int(time()) - 1))
        await session.commit()
    _, second_headers = await _login(client)
    assert (await client.post("/api/v1/web/auth/logout", headers=second_headers)).status_code == 200
    assert (await client.get("/api/v1/dashboard/summary", headers=first_headers)).status_code == 401
    async with db_mod.get_async_session() as session:
        rows = list((await session.execute(select(RevokedWebSession))).scalars())
    assert len(rows) == 3
    assert any(row.session_hash == "0" * 64 for row in rows)


async def test_clock_skew_or_rollback_cannot_restore_a_revoked_session(client, monkeypatch):
    clock = [time()]
    monkeypatch.setattr(security.time, "time", lambda: clock[0])
    # Also replace the pre-fix cleaner's separately imported wall-clock function.
    monkeypatch.setattr(web_sessions, "time", lambda: clock[0], raising=False)
    token, first_headers = await _login(client)
    expires_at = verify_session(token, signing_key=SIGNING_KEY)["exp"]
    assert isinstance(expires_at, int)
    assert (await client.post("/api/v1/web/auth/logout", headers=first_headers)).status_code == 200
    clock[0] += 5
    _, second_headers = await _login(client)
    # Another instance is ahead of the revoked token's expiry, but its own login is valid.
    clock[0] = expires_at + 1
    assert (await client.post("/api/v1/web/auth/logout", headers=second_headers)).status_code == 200
    # A slower instance (or a clock rollback) would consider the first signature unexpired.
    clock[0] = expires_at - 1
    assert (await client.get("/api/v1/dashboard/summary", headers=first_headers)).status_code == 401


async def test_revocation_read_failure_closes_all_admin_auth_paths(client, monkeypatch):
    _, headers = await _login(client)

    async def unavailable(_token):
        raise SQLAlchemyError("isolated storage failure")

    monkeypatch.setattr(WebSessionService, "is_revoked", unavailable)
    for response in [
        await client.get("/api/v1/dashboard/summary", headers=headers),
        await client.get("/api/v1/web/auth/session", headers=headers),
        await client.post("/api/v1/web/auth/logout", headers=headers),
    ]:
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "WEB_SESSION_STORAGE_UNAVAILABLE"
        assert "set-cookie" not in response.headers


async def test_revocation_write_failure_does_not_claim_logout(client, monkeypatch):
    _, headers = await _login(client)

    async def unavailable(_token, *, expires_at):
        raise SQLAlchemyError("isolated storage failure")

    monkeypatch.setattr(WebSessionService, "revoke", unavailable)
    response = await client.post("/api/v1/web/auth/logout", headers=headers)
    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert (await client.get("/api/v1/web/auth/session", headers=headers)).json() == {
        "data": {"authenticated": True}
    }
