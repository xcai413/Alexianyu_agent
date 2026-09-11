"""WS Token、注册帧与加密缓存测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.cli.commands.auth import _persist_qr_login_session
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, WsCredential, database as db_mod, get_async_session
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.qr_login import QRLoginSession, QrStatus
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.protocol.ws_auth import (
    WsAuthError,
    WsCredentials,
    WsTokenProvider,
    build_registration_frame,
    build_sync_frame,
    generate_device_id,
)


@pytest.fixture
async def ws_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "ws-auth.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", key)
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    async with get_async_session() as session:
        session.add(Account(account_id="acc-ws", enabled=True))
        await session.commit()
    yield key
    await db_mod.async_engine.dispose()
    reset_settings_cache()


class StubTokenProvider(WsTokenProvider):
    def __init__(self, responses: list[httpx.Response], signer: CookieSigner) -> None:
        super().__init__(signer)
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    async def _request_token(self, cookie: str, device_id: str):
        self.calls.append((cookie, device_id))
        response = self.responses.pop(0)
        from xianyu_agent.protocol.ws_auth import _merge_set_cookies  # noqa: PLC0415

        return response, _merge_set_cookies(cookie, response)


@pytest.mark.asyncio
async def test_token_is_encrypted_and_cached_with_stable_device(ws_db) -> None:
    signer = CookieSigner()
    await signer.save_cookie("acc-ws", "unb=user-1; _m_h5_tk=seed_x; cookie2=c2")
    response = httpx.Response(
        200,
        json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "secret-access"}},
    )
    provider = StubTokenProvider([response], signer)

    first = await provider.get_credentials("acc-ws", force_refresh=True)
    second = await provider.get_credentials("acc-ws")

    assert first.access_token == "secret-access"
    assert second.device_id == first.device_id
    assert len(provider.calls) == 1
    async with get_async_session() as session:
        row = (await session.execute(select(WsCredential))).scalar_one()
    assert row.encrypted_token != "secret-access"
    assert "secret-access" not in row.encrypted_token


@pytest.mark.asyncio
async def test_default_get_requires_prepared_current_credential_without_network(ws_db) -> None:
    signer = CookieSigner()
    await signer.save_cookie("acc-ws", "unb=user-1; _m_h5_tk=seed_x; cookie2=c2")
    provider = StubTokenProvider(
        [
            httpx.Response(
                200,
                json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "unused"}},
            )
        ],
        signer,
    )

    with pytest.raises(WsAuthError, match="已准备"):
        await provider.get_credentials("acc-ws")

    assert provider.calls == []
    status = await provider.status("acc-ws")
    assert status.token_cached is False


@pytest.mark.asyncio
async def test_current_read_does_not_refresh_or_rewrite_durable_credential(ws_db) -> None:
    signer = CookieSigner()
    await signer.save_cookie("acc-ws", "unb=user-1; _m_h5_tk=seed_x; cookie2=c2")
    provider = StubTokenProvider(
        [
            httpx.Response(
                200,
                json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "prepared"}},
            )
        ],
        signer,
    )
    prepared = await provider.get_credentials("acc-ws", force_refresh=True)
    async with get_async_session() as session:
        before = (await session.execute(select(WsCredential))).scalar_one()
        before_snapshot = (before.encrypted_token, before.device_id, before.expires_at)

    current = await provider.get_credentials("acc-ws")

    async with get_async_session() as session:
        after = (await session.execute(select(WsCredential))).scalar_one()
        after_snapshot = (after.encrypted_token, after.device_id, after.expires_at)
    assert current == prepared
    assert provider.calls
    assert len(provider.calls) == 1
    assert after_snapshot == before_snapshot


@pytest.mark.asyncio
async def test_missing_mtop_token_retries_after_set_cookie_bootstrap(ws_db) -> None:
    signer = CookieSigner()
    await signer.save_cookie("acc-ws", "unb=user-1; cookie2=c2")
    provider = StubTokenProvider(
        [
            httpx.Response(
                200,
                headers={"set-cookie": "_m_h5_tk=seed_bootstrap; Path=/"},
                json={"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
            ),
            httpx.Response(
                200,
                json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "access-2"}},
            ),
        ],
        signer,
    )

    credentials = await provider.get_credentials("acc-ws", force_refresh=True)

    assert credentials.access_token == "access-2"
    assert len(provider.calls) == 2
    assert "_m_h5_tk=seed_bootstrap" in provider.calls[1][0]
    saved = await signer.load_cookie_value("acc-ws")
    assert saved is not None
    assert "_m_h5_tk=seed_bootstrap" in saved


@pytest.mark.asyncio
async def test_device_id_survives_failed_token_request(ws_db) -> None:
    signer = CookieSigner()
    await signer.save_cookie("acc-ws", "unb=user-1; _m_h5_tk=seed_x; cookie2=c2")
    failure = lambda: httpx.Response(  # noqa: E731
        200, json={"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]}
    )
    first_provider = StubTokenProvider([failure()], signer)
    with pytest.raises(WsAuthError, match="Session过期"):
        await first_provider.get_credentials("acc-ws", force_refresh=True)
    second_provider = StubTokenProvider([failure()], signer)
    with pytest.raises(WsAuthError, match="Session过期"):
        await second_provider.get_credentials("acc-ws", force_refresh=True)
    assert first_provider.calls[0][1] == second_provider.calls[0][1]


@pytest.mark.asyncio
async def test_new_cookie_invalidates_token_but_preserves_device(ws_db) -> None:
    signer = CookieSigner()
    await signer.save_cookie("acc-ws", "unb=user-1; _m_h5_tk=old_seed")
    provider = StubTokenProvider(
        [
            httpx.Response(
                200,
                json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "old-access"}},
            )
        ],
        signer,
    )
    await provider.get_credentials("acc-ws", force_refresh=True)
    before = await provider.status("acc-ws")
    assert before.valid is True

    await signer.save_cookie("acc-ws", "unb=user-1; _m_h5_tk=new_seed")
    after = await provider.status("acc-ws")
    assert after.token_cached is False
    assert after.valid is False
    assert after.device_id_masked == before.device_id_masked
    assert before.device_id_masked is not None
    assert before.device_id_masked.startswith("device#")


@pytest.mark.asyncio
async def test_qr_login_persists_cookie_and_im_token_in_one_step(ws_db) -> None:
    signer = CookieSigner()
    provider = StubTokenProvider(
        [
            httpx.Response(
                200,
                json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "qr-access"}},
            )
        ],
        signer,
    )
    session = QRLoginSession(
        status=QrStatus.SUCCESS,
        unb="user-1",
        cookies={"unb": "user-1", "_m_h5_tk": "seed_x", "cookie2": "c2"},
    )

    await _persist_qr_login_session("acc-ws", "扫码测试", session, token_provider=provider)

    assert provider.calls[0][0].startswith("unb=user-1")
    status = await provider.status("acc-ws")
    assert status.token_cached is True
    assert status.valid is True


@pytest.mark.asyncio
async def test_cookie_fingerprint_does_not_expose_full_user_id(ws_db) -> None:
    signer = CookieSigner()
    await signer.save_cookie("acc-ws", "unb=user-identity-secret; _m_h5_tk=token_secret")
    fingerprint = await signer.fingerprint("acc-ws")
    assert fingerprint is not None
    output = json.dumps(fingerprint)
    assert "user-identity-secret" not in output
    assert "token_secret" not in output
    assert fingerprint["has_unb"] is True
    assert fingerprint["unb_masked"] is not None


def test_registration_and_sync_frames_contain_required_fields() -> None:
    credentials = WsCredentials(
        access_token="access",
        device_id=generate_device_id("user-1"),
        user_id="user-1",
        expires_at=datetime.now(UTC),
    )
    registration = build_registration_frame(credentials)
    sync = build_sync_frame(now_ms=1700000000000)
    assert registration["lwp"] == "/reg"
    assert registration["headers"]["token"] == "access"
    assert registration["headers"]["did"].endswith("-user-1")
    assert sync["lwp"] == "/r/SyncStatus/ackDiff"
    assert sync["body"][0]["timestamp"] == 1700000000000
    assert sync["body"][0]["pts"] == 1700000000000000


@pytest.mark.asyncio
async def test_auth_failure_enters_error_and_uses_long_cooldown(
    ws_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingProvider:
        async def get_credentials(self, _account_id: str):
            from xianyu_agent.protocol.ws_auth import WsAuthError  # noqa: PLC0415

            raise WsAuthError("Session过期")

    client = WsClient(
        "acc-ws",
        config=ClientConfig(
            ws_url="ws://unused",
            auth_retry_delay_s=60,
            min_backoff_s=0.001,
            max_backoff_s=0.001,
        ),
        token_provider=FailingProvider(),
    )
    delays: list[float] = []

    async def capture_delay(seconds: float) -> None:
        delays.append(seconds)
        client._stop.set()

    monkeypatch.setattr(client, "_sleep_or_stop", capture_delay)
    await client._run_forever()
    assert delays == [60]
    assert client._reconnect_attempts == 1
