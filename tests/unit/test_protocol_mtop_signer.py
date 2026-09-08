"""Regression contracts for the canonical MTOP signer migration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, Cookie, WsCredential, database as db_mod, get_async_session
from xianyu_agent.protocol import signer as legacy_signer
from xianyu_agent.protocol.mtop import signer as canonical_signer


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


def test_legacy_signer_reexports_canonical_contract() -> None:
    names = (
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
    )
    for name in names:
        assert getattr(legacy_signer, name) is getattr(canonical_signer, name)


def test_compute_sign_preserves_known_wire_vector() -> None:
    raw = b"seed&1700000000000&34839810&data"
    expected = hashlib.md5(raw).hexdigest()
    assert (
        canonical_signer.compute_sign("seed", 1700000000000, "34839810", "data")
        == expected
    )


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
