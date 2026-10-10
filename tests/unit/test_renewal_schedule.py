"""Tests for scheduled long-login renewal (refresh_after)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xianyu_agent.application.session.renewal import ACTION_RENEWED, RenewalReport
from xianyu_agent.application.session.renewal_schedule import (
    DEFAULT_LEAD_TIME,
    LongLoginRenewalScheduler,
    renewal_due,
)

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


def _cookie(*, hours_until_expiry: float | None = 48, fatigue_hours: float | None = None) -> str:
    parts = ["unb=1"]
    if hours_until_expiry is not None:
        expires = int((NOW + timedelta(hours=hours_until_expiry)).timestamp() * 1000)
        parts.append(f"havana_lgc_exp={expires}")
    if fatigue_hours is not None:
        parts.append(f"sdkSilent={int((NOW + timedelta(hours=fatigue_hours)).timestamp() * 1000)}")
    return "; ".join(parts)


def test_due_when_inside_the_lead_window() -> None:
    decision = renewal_due("acc-1", _cookie(hours_until_expiry=6), now=NOW)

    assert decision.due is True
    assert "提前续期" in decision.reason
    assert decision.seconds_until_expiry == pytest.approx(6 * 3600, abs=1)


def test_not_due_when_far_from_expiry() -> None:
    decision = renewal_due("acc-1", _cookie(hours_until_expiry=72), now=NOW)

    assert decision.due is False
    assert "尚未进入" in decision.reason


def test_fatigue_window_suppresses_the_attempt() -> None:
    decision = renewal_due("acc-1", _cookie(hours_until_expiry=6, fatigue_hours=2), now=NOW)

    assert decision.due is False
    assert "疲劳窗口" in decision.reason


def test_expired_and_missing_cookies_are_still_flagged_for_humans() -> None:
    expired = renewal_due("acc-1", _cookie(hours_until_expiry=-1), now=NOW)
    missing = renewal_due("acc-1", "", now=NOW)
    unparsable = renewal_due("acc-1", "unb=1; havana_lgc_exp=garbage", now=NOW)

    assert expired.due is True
    assert "已过期" in expired.reason
    assert missing.due is True
    assert "没有可用 Cookie" in missing.reason
    assert unparsable.due is True
    assert "不可解析" in unparsable.reason


class FakeService:
    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_for = fail_for or set()

    async def renew(self, account_id: str, *, now: object = None) -> RenewalReport:
        self.calls.append(account_id)
        if account_id in self.fail_for:
            msg = "boom"
            raise RuntimeError(msg)
        return RenewalReport(
            account_id=account_id, action=ACTION_RENEWED, success=True, reason="续期成功"
        )


def _scheduler(service: FakeService, cookies: dict[str, str], **kwargs: object):
    async def reader(account_id: str) -> str:
        return cookies.get(account_id, "")

    return LongLoginRenewalScheduler(
        service=service,  # type: ignore[arg-type]
        cookie_reader=reader,
        clock=lambda: NOW,
        lead_time=DEFAULT_LEAD_TIME,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_only_due_accounts_are_renewed_and_failures_are_contained() -> None:
    service = FakeService(fail_for={"acc-fail"})
    cookies = {
        "acc-due": _cookie(hours_until_expiry=3),
        "acc-fresh": _cookie(hours_until_expiry=96),
        "acc-fail": _cookie(hours_until_expiry=2),
        "acc-after-fail": _cookie(hours_until_expiry=1),
    }
    scheduler = _scheduler(service, cookies)

    reports = await scheduler.run_once(["acc-due", "acc-fresh", "acc-fail", "acc-after-fail"])

    assert service.calls == ["acc-due", "acc-fail", "acc-after-fail"]
    assert [report.account_id for report in reports] == ["acc-due", "acc-after-fail"]


@pytest.mark.asyncio
async def test_min_interval_prevents_repeated_attempts_for_the_same_account() -> None:
    service = FakeService()
    scheduler = _scheduler(service, {"acc-1": _cookie(hours_until_expiry=1)})

    first = await scheduler.run_once(["acc-1"])
    second = await scheduler.run_once(["acc-1"])

    assert len(first) == 1
    assert second == ()
    assert service.calls == ["acc-1"]


@pytest.mark.asyncio
async def test_scheduler_requires_an_account_source() -> None:
    service = FakeService()
    scheduler = _scheduler(service, {})

    with pytest.raises(ValueError, match="未提供账号来源"):
        await scheduler.run_once()


@pytest.mark.asyncio
async def test_scheduler_lists_accounts_when_none_are_given() -> None:
    service = FakeService()

    async def reader(_account_id: str) -> str:
        return _cookie(hours_until_expiry=2)

    async def lister() -> list[str]:
        return ["acc-1", "acc-2"]

    scheduler = LongLoginRenewalScheduler(
        service=service,  # type: ignore[arg-type]
        cookie_reader=reader,
        account_lister=lister,
        clock=lambda: NOW,
    )

    reports = await scheduler.run_once()

    assert [report.account_id for report in reports] == ["acc-1", "acc-2"]


@pytest.mark.asyncio
async def test_forced_pass_renews_a_fresh_cookie() -> None:
    service = FakeService()
    scheduler = _scheduler(service, {"acc-fresh": _cookie(hours_until_expiry=96)})

    reports = await scheduler.run_once(["acc-fresh"], force=True)

    assert [report.account_id for report in reports] == ["acc-fresh"]
    assert service.calls == ["acc-fresh"]
