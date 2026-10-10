"""Tests for the long-login renewal orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.application.session.renewal import (
    ACTION_BLOCKED,
    ACTION_FAILED,
    ACTION_RENEWED,
    ACTION_SKIPPED,
    AUDIT_ACTION,
    LongLoginRenewalService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.account import risk as account_risk
from xianyu_agent.protocol.renew import (
    MODE_HAVANA,
    SKIP_FATIGUE,
    SKIP_LONG_LOGIN_EXPIRED,
    RenewError,
    RenewOutcome,
)

COOKIE = "unb=1; havana_lgc_exp=123; cookie2=old"


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "renewal.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


class FakeSigner:
    def __init__(self, cookie: str | None = COOKIE, *, save_ok: bool = True) -> None:
        self.cookie = cookie
        self.save_ok = save_ok
        self.saved: list[tuple[str, str]] = []

    async def load_cookie_value(self, _account_id: str) -> str:
        return self.cookie or ""

    async def save_cookie(self, account_id: str, cookie: str) -> bool:
        self.saved.append((account_id, cookie))
        return self.save_ok


class FakeRenewer:
    def __init__(
        self, *, outcome: RenewOutcome | None = None, error: Exception | None = None
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.calls = 0

    async def renew(
        self, _cookie: str, *, now: object = None, session_expired: bool = False
    ) -> RenewOutcome:
        self.calls += 1
        _ = now, session_expired
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


def _service(signer: FakeSigner, renewer: FakeRenewer) -> LongLoginRenewalService:
    return LongLoginRenewalService(signer=signer, renewer=renewer)  # type: ignore[arg-type]


async def _audits() -> list[AuditLog]:
    async with db_mod.get_async_session() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == AUDIT_ACTION)
                    .order_by(AuditLog.id.asc())
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_success_writes_back_and_audits_names_only(clean_db) -> None:
    signer = FakeSigner()
    renewer = FakeRenewer(
        outcome=RenewOutcome(
            mode=MODE_HAVANA,
            success=True,
            cookie="unb=1; havana_lgc_exp=456; cookie2=new",
            changed_names=("cookie2", "havana_lgc_exp"),
            message="续期成功",
        )
    )

    report = await _service(signer, renewer).renew("acc-1")
    audits = await _audits()

    assert report.action == ACTION_RENEWED
    assert report.success is True
    assert report.changed_names == ("cookie2", "havana_lgc_exp")
    assert signer.saved == [("acc-1", "unb=1; havana_lgc_exp=456; cookie2=new")]
    assert len(audits) == 1
    assert audits[0].result == ACTION_RENEWED
    assert audits[0].params["changed_names"] == ["cookie2", "havana_lgc_exp"]
    dumped = str(audits[0].params)
    assert "cookie2=new" not in dumped
    assert "unb=1" not in dumped


@pytest.mark.asyncio
async def test_cooling_account_is_never_renewed(clean_db) -> None:
    await account_risk.open_user_validate("acc-1")
    signer = FakeSigner()
    renewer = FakeRenewer(outcome=RenewOutcome(success=True, cookie="x"))

    report = await _service(signer, renewer).renew("acc-1")
    audits = await _audits()

    assert report.action == ACTION_BLOCKED
    assert report.need_human_login is True
    assert report.cooldown_remaining_seconds is not None
    assert renewer.calls == 0
    assert signer.saved == []
    assert audits[0].result == ACTION_BLOCKED


@pytest.mark.asyncio
async def test_expired_circuit_also_blocks_until_human_recovers(clean_db) -> None:
    await account_risk.open_user_validate("acc-1", now=datetime.now(UTC) - timedelta(hours=1))
    signer = FakeSigner()
    renewer = FakeRenewer(outcome=RenewOutcome(success=True, cookie="x"))

    report = await _service(signer, renewer).renew("acc-1")

    assert report.action == ACTION_BLOCKED
    assert "尚未解除" in report.reason
    assert renewer.calls == 0


@pytest.mark.asyncio
async def test_fatigue_skip_does_not_call_or_write(clean_db) -> None:
    signer = FakeSigner()
    renewer = FakeRenewer(
        outcome=RenewOutcome(
            skipped=True,
            skip_reason=SKIP_FATIGUE,
            cookie=COOKIE,
            message="sdkSilent 疲劳窗口内,跳过静默续期",
        )
    )

    report = await _service(signer, renewer).renew("acc-1")

    assert report.action == ACTION_SKIPPED
    assert report.success is False
    assert report.need_human_login is False
    assert signer.saved == []


@pytest.mark.asyncio
async def test_long_login_expired_reports_human_login(clean_db) -> None:
    signer = FakeSigner()
    renewer = FakeRenewer(
        outcome=RenewOutcome(
            skipped=True,
            skip_reason=SKIP_LONG_LOGIN_EXPIRED,
            cookie=COOKIE,
            message="长登录凭证已过期,需要人工重新登录",
            need_human_login=True,
        )
    )

    report = await _service(signer, renewer).renew("acc-1")

    assert report.action == ACTION_SKIPPED
    assert report.need_human_login is True
    assert signer.saved == []


@pytest.mark.asyncio
async def test_failures_keep_the_existing_cookie(clean_db) -> None:
    signer = FakeSigner()
    failing = FakeRenewer(error=RenewError("续期网络请求失败(ConnectError)"))

    report = await _service(signer, failing).renew("acc-1")
    audits = await _audits()

    assert report.action == ACTION_FAILED
    assert report.need_human_login is True
    assert signer.saved == []
    assert audits[0].result == ACTION_FAILED
    assert audits[0].error is not None


@pytest.mark.asyncio
async def test_failed_business_result_keeps_partial_cookie_update(clean_db) -> None:
    signer = FakeSigner()
    renewer = FakeRenewer(
        outcome=RenewOutcome(
            mode=MODE_HAVANA,
            success=False,
            cookie="unb=1; havana_lgc_exp=456; cookie2=partial",
            changed_names=("cookie2",),
            message="平台未确认续期完成",
            need_human_login=True,
        )
    )

    report = await _service(signer, renewer).renew("acc-1")

    assert report.action == ACTION_FAILED
    assert signer.saved == [("acc-1", "unb=1; havana_lgc_exp=456; cookie2=partial")]


@pytest.mark.asyncio
async def test_missing_cookie_and_failed_persistence_are_reported(clean_db) -> None:
    no_cookie = FakeSigner(cookie=None)
    renewer = FakeRenewer(outcome=RenewOutcome(success=True, cookie="x"))

    missing = await _service(no_cookie, renewer).renew("acc-1")

    assert missing.action == ACTION_FAILED
    assert "没有可用 Cookie" in missing.reason
    assert renewer.calls == 0

    signer = FakeSigner(save_ok=False)
    stuck = await _service(
        signer,
        FakeRenewer(outcome=RenewOutcome(success=True, cookie="unb=9", changed_names=("unb",))),
    ).renew("acc-1")

    assert stuck.action == ACTION_FAILED
    assert "写回失败" in stuck.reason
    assert signer.saved == [("acc-1", "unb=9")]
