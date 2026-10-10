"""Regression contracts for the canonical MTOP signer migration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import (
    Account,
    AuditLog,
    Cookie,
    WsCredential,
    database as db_mod,
    get_async_session,
)
from xianyu_agent.domain import worker_risk
from xianyu_agent.protocol import signer as legacy_signer
from xianyu_agent.protocol.mtop import signer as canonical_signer
from xianyu_agent.protocol.mtop.cookies import BrowserCookie
from xianyu_agent.protocol.mtop.endpoints.item_detail import XianyuItemDetailClient
from xianyu_agent.protocol.mtop.endpoints.item_polish import XianyuItemPolishClient
from xianyu_agent.protocol.mtop.endpoints.items import XianyuItemsClient
from xianyu_agent.protocol.mtop.endpoints.orders import XianyuOrdersClient
from xianyu_agent.protocol.mtop.request_gate import (
    MtopRequestsPausedError,
    load_request_cookie,
    observe_mtop_response,
)


@pytest.fixture
async def signer_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "mtop-signer.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", key)
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    async with get_async_session() as session:
        session.add(Account(account_id="acc-mtop", enabled=True))
        await session.commit()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def test_legacy_signer_reexports_canonical_signer_contract() -> None:
    names = (
        "APP_KEY",
        "SIGN_VERSION",
        "CookieSigner",
        "MtopHeaders",
        "compute_sign",
        "derive_token_seed",
        "extract_cookie_field",
        "extract_mtop_token",
        "make_headers",
    )
    for name in names:
        assert getattr(legacy_signer, name) is getattr(canonical_signer, name)


def test_legacy_generic_error_helper_remains_compatible() -> None:
    error = legacy_signer.make_error("acc-mtop", "AUTH", "safe message")

    assert error.account_id == "acc-mtop"
    assert error.code == "AUTH"
    assert error.message == "safe message"
    assert error.event_id
    assert not hasattr(canonical_signer, "make_error")


def test_compute_sign_preserves_known_wire_vector() -> None:
    raw = b"seed&1700000000000&34839810&data"
    expected = hashlib.md5(raw).hexdigest()
    assert canonical_signer.compute_sign("seed", 1700000000000, "34839810", "data") == expected


def test_make_headers_preserves_wire_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canonical_signer.time, "time", lambda: 1700000000.0)
    headers = canonical_signer.make_headers("seed_suffix", data="payload")

    assert headers.app_key == "34839810"
    assert headers.x_t == "1700000000000"
    assert headers.x_sign == canonical_signer.compute_sign(
        "seed", 1700000000000, "34839810", "payload"
    )
    assert headers.token == "seed_suffix"


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        ("unb=u; _m_h5_tk=seed_a; cookie2=c", "seed_a"),
        ("unb=u; m_h5_tk=seed_b; cookie2=c", "seed_b"),
        ("unb=u; cookie2=c", None),
    ],
)
def test_extract_mtop_token_preserves_observed_cookie_spellings(
    cookie: str, expected: str | None
) -> None:
    assert canonical_signer.extract_mtop_token(cookie) == expected


@pytest.mark.asyncio
async def test_cookie_save_is_encrypted_and_invalidates_token_without_rotating_device(
    signer_db,
) -> None:
    signer = canonical_signer.CookieSigner()
    first_cookie = "unb=user-secret; _m_h5_tk=old-secret; cookie2=c1"
    assert await signer.save_cookie("acc-mtop", first_cookie) is True

    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == "acc-mtop"))
        ).scalar_one()
        session.add(
            WsCredential(
                account_id=account.id,
                encrypted_token=signer.fernet.encrypt(b"old-access-secret").decode("utf-8"),
                device_id="stable-device-id",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()

    new_cookie = "unb=user-secret; _m_h5_tk=new-secret; cookie2=c2"
    assert await signer.save_cookie("acc-mtop", new_cookie) is True

    async with get_async_session() as session:
        credential = (await session.execute(select(WsCredential))).scalar_one()
        cookie_rows = (await session.execute(select(Cookie))).scalars().all()

    expires_at = credential.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    assert credential.device_id == "stable-device-id"
    assert signer.fernet.decrypt(credential.encrypted_token.encode("utf-8")) == b""
    assert expires_at <= datetime.now(UTC)
    assert len(cookie_rows) == 2
    for row in cookie_rows:
        assert "user-secret" not in row.encrypted_value
        assert "old-secret" not in row.encrypted_value
        assert "new-secret" not in row.encrypted_value
    assert await signer.load_cookie_value("acc-mtop") == new_cookie
    async with get_async_session() as session:
        audits = list((await session.scalars(select(AuditLog).order_by(AuditLog.id))).all())
    assert len(audits) == 2
    assert audits[-1].action == "credential_cookie_saved"
    assert audits[-1].params["previous_ws_token_invalidated"] is True
    assert audits[0].params["cookie_digest"] != audits[-1].params["cookie_digest"]
    assert "new-secret" not in json.dumps(audits[-1].params)
    assert (await signer.fingerprint("acc-mtop"))["cookie_digest"] == audits[-1].params[
        "cookie_digest"
    ]


@pytest.mark.asyncio
async def test_saved_browser_snapshot_round_trips_and_latest_cookie_is_authoritative(signer_db):
    signer = canonical_signer.CookieSigner()
    original = [BrowserCookie("cookie2", "snapshot-secret", ".goofish.com", http_only=True)]
    assert await signer.save_cookie("acc-mtop", "cookie2=first", snapshot=original)
    assert await signer.load_cookie_snapshot("acc-mtop") == original
    refreshed = [BrowserCookie("cookie2", "refreshed-secret", ".goofish.com", secure=True)]
    assert await signer.save_cookie_snapshot("acc-mtop", refreshed)
    assert await signer.load_cookie_snapshot("acc-mtop") == refreshed
    assert await signer.save_cookie("acc-mtop", "cookie2=second")
    assert await signer.load_cookie_snapshot("acc-mtop") is None


@pytest.mark.asyncio
async def test_mtop_gate_survives_database_reopen_and_expired_cooldown(signer_db):
    signer = canonical_signer.CookieSigner()
    assert await signer.save_cookie("acc-mtop", "cookie2=local-test")
    await worker_risk.open_user_validate("acc-mtop", now=datetime.now(UTC) - timedelta(days=1))
    await db_mod.async_engine.dispose()
    db_mod.reset_engine()

    class ForbiddenSigner:
        async def load_cookie_value(self, _account_id):
            pytest.fail("paused requests must not load credentials")

    with pytest.raises(MtopRequestsPausedError):
        await load_request_cookie(ForbiddenSigner(), "acc-mtop")
    assert await signer.load_cookie_value("acc-mtop") == "cookie2=local-test"
    current = await worker_risk.get("acc-mtop")
    assert await worker_risk.clear_after_refresh("acc-mtop", expected_revision=current.revision)
    assert await load_request_cookie(signer, "acc-mtop") == "cookie2=local-test"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["orders", "detail", "polish"])
async def test_paused_read_and_write_endpoints_never_open_http(signer_db, monkeypatch, operation):
    signer = canonical_signer.CookieSigner()
    assert await signer.save_cookie("acc-mtop", "unb=test; _m_h5_tk=test_suffix; cookie2=test")
    await worker_risk.open_user_validate("acc-mtop", now=datetime.now(UTC) - timedelta(days=1))

    def forbidden_http(*_args, **_kwargs):
        pytest.fail("paused account must not open HTTP connections")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_http)
    if operation == "orders":
        request = XianyuOrdersClient(signer).fetch_sold_page("acc-mtop")
    elif operation == "detail":
        request = XianyuItemDetailClient(signer).fetch_item_detail("acc-mtop", "123")
    else:
        request = XianyuItemPolishClient(signer).polish_item("acc-mtop", "123")
    with pytest.raises(MtopRequestsPausedError):
        await request


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["items", "orders"])
@pytest.mark.parametrize("code", ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR", "FAIL_SYS_FLOW_LIMIT"])
async def test_risk_bootstrap_is_not_retried_and_persists_account_pause(
    signer_db, monkeypatch, endpoint, code
):
    signer = canonical_signer.CookieSigner()
    assert await signer.save_cookie("acc-mtop", "unb=local-user; cookie2=local-cookie")
    client = XianyuItemsClient(signer) if endpoint == "items" else XianyuOrdersClient(signer)
    calls = []

    async def risk_response(*args):
        calls.append(1)
        return httpx.Response(
            200,
            json={"ret": [code]},
            headers={"set-cookie": "_m_h5_tk=local-secret_suffix; Path=/;"},
            request=httpx.Request("POST", "https://unit.test"),
        )

    method = "_request_list" if endpoint == "items" else "_request_sold_orders"
    monkeypatch.setattr(client, method, risk_response)
    fetch = client.fetch_on_sale_page if endpoint == "items" else client.fetch_sold_page
    with pytest.raises(MtopRequestsPausedError):
        await fetch("acc-mtop")
    assert len(calls) == 1
    risk = await worker_risk.get("acc-mtop")
    assert risk is not None
    assert risk.code == code
    with pytest.raises(MtopRequestsPausedError):
        await fetch("acc-mtop")
    assert len(calls) == 1
    assert await signer.load_cookie_value("acc-mtop") == "unb=local-user; cookie2=local-cookie"


@pytest.mark.asyncio
async def test_normal_mtop_business_error_and_message_text_do_not_trip_risk(signer_db):
    response = httpx.Response(
        200, json={"ret": ["FAIL_BIZ_ITEM_NOT_FOUND"], "data": {"text": "FAIL_SYS_USER_VALIDATE"}}
    )
    await observe_mtop_response("acc-mtop", response)
    assert await worker_risk.get("acc-mtop") is None


@pytest.mark.asyncio
async def test_signed_write_risk_response_persists_gate_before_business_parser(
    signer_db, monkeypatch
):
    signer = canonical_signer.CookieSigner()
    assert await signer.save_cookie("acc-mtop", "unb=local-user; _m_h5_tk=local_suffix")
    client = XianyuItemPolishClient(signer)
    requests = []

    class MockHttp:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *args, **kwargs):
            requests.append(1)
            return httpx.Response(200, json={"ret": ["FAIL_SYS_FLOW_LIMIT"]})

    monkeypatch.setattr(httpx, "AsyncClient", MockHttp)
    with pytest.raises(MtopRequestsPausedError):
        await client.polish_item("acc-mtop", "123")
    risk = await worker_risk.get("acc-mtop")
    assert risk is not None
    assert risk.code == "FAIL_SYS_FLOW_LIMIT"
    with pytest.raises(MtopRequestsPausedError):
        await client.polish_item("acc-mtop", "123")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_fingerprint_remains_secret_free(signer_db) -> None:
    signer = canonical_signer.CookieSigner()
    cookie = "unb=user-identity-secret; _m_h5_tk=token_secret; cookie2=c2"
    assert await signer.save_cookie("acc-mtop", cookie) is True

    fingerprint = await signer.fingerprint("acc-mtop")

    assert fingerprint is not None
    rendered = json.dumps(fingerprint)
    assert "user-identity-secret" not in rendered
    assert "token_secret" not in rendered
    assert fingerprint["has_unb"] is True
    assert fingerprint["has_m_h5_tk"] is True
    assert fingerprint["unb_masked"] is not None
    assert fingerprint["m_h5_tk_digest"] is not None
