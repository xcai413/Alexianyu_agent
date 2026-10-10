"""Run the same administrator-session invariants on each dedicated CI database."""

from __future__ import annotations

import inspect
import os

import httpx
import pytest
from sqlalchemy import delete

from tests.unit import test_web_session_revocation as cases
from xianyu_agent.config import get_settings, reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.infrastructure.database.models import RevokedWebSession
from xianyu_agent.interfaces.web.auth.security import hash_password

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(monkeypatch):
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend
    monkeypatch.setenv("XIANYU_WEB_ADMIN_PASSWORD_HASH", hash_password(cases.PASSWORD))
    monkeypatch.setenv("XIANYU_WEB_SESSION_SECRET", cases.SIGNING_KEY)
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    async with db_mod.get_async_session() as session:
        # This matrix, like the database smoke test, must use a dedicated test database.
        await session.execute(delete(RevokedWebSession))
        await session.commit()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cases._app()), base_url="http://test"
        ) as client:
            yield client
    finally:
        await db_mod.async_engine.dispose()
        reset_settings_cache()


@pytest.mark.parametrize(
    "case",
    [
        cases.test_logout_revocation_survives_restart_and_new_app,
        cases.test_logout_of_one_login_preserves_another_active_login,
        cases.test_bad_csrf_does_not_revoke_or_delete_valid_session,
        cases.test_concurrent_copied_logout_commits_one_revocation,
        cases.test_expired_records_do_not_remove_live_revocations,
        cases.test_clock_skew_or_rollback_cannot_restore_a_revoked_session,
        cases.test_revocation_read_failure_closes_all_admin_auth_paths,
        cases.test_revocation_write_failure_does_not_claim_logout,
    ],
    ids=lambda case: case.__name__.removeprefix("test_"),
)
async def test_session_revocation_contract_across_backend(client, monkeypatch, case):
    if "monkeypatch" in inspect.signature(case).parameters:
        await case(client, monkeypatch)
    else:
        await case(client)
