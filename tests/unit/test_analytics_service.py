"""运营分析:窗口指标、自动化健康与账号隔离。"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.application.analytics import AnalyticsService
from xianyu_agent.application.automation import PlatformAutomationService
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.infrastructure.database.models import (
    Account as AccountRow,
    Conversation as ConversationRow,
    Item as ItemRow,
    Message as MessageRow,
    Order as OrderRow,
)

runner = CliRunner()
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
async def analytics_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "analytics.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_order(
    account_id: str,
    order_id: str,
    *,
    status: str,
    amount: float,
    placed_at: datetime,
    paid_at: datetime | None,
    delivered_at: datetime | None = None,
) -> None:
    async with get_async_session() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == account_id).limit(1)
            )
        ).scalar_one()
        session.add(
            OrderRow(
                account_id=account.id,
                order_id=order_id,
                item_id="I-1",
                item_title="商品",
                amount=amount,
                quantity=1,
                status=status,
                placed_at=placed_at,
                paid_at=paid_at,
                delivered_at=delivered_at,
            )
        )


async def _seed_message(account_id: str, *, direction: str, received_at: datetime) -> None:
    async with get_async_session() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == account_id).limit(1)
            )
        ).scalar_one()
        # 消息必须挂在会话下:镜像模型从会话化迁移后就要求 conversation_id。
        conversation = (
            await session.execute(
                select(ConversationRow)
                .where(
                    ConversationRow.account_id == account.id,
                    ConversationRow.external_conversation_id == "chat-1",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if conversation is None:
            conversation = ConversationRow(account_id=account.id, external_conversation_id="chat-1")
            session.add(conversation)
            await session.flush()
        session.add(
            MessageRow(
                account_id=account.id,
                conversation_id=conversation.id,
                message_id=f"m-{received_at.timestamp()}-{direction}",
                chat_id="chat-1",
                direction=direction,
                content_type="text",
                content="你好",
                received_at=received_at,
            )
        )


async def test_report_counts_only_inside_each_window(analytics_db: None) -> None:
    await _seed_order(
        "acc-1",
        "O-today",
        status="paid",
        amount=10.0,
        placed_at=NOW - timedelta(hours=2),
        paid_at=NOW - timedelta(hours=2),
    )
    await _seed_order(
        "acc-1",
        "O-lastweek",
        status="delivered",
        amount=90.0,
        placed_at=NOW - timedelta(days=5),
        paid_at=NOW - timedelta(days=5),
        delivered_at=NOW - timedelta(days=4),
    )
    await _seed_order(
        "acc-1",
        "O-old",
        status="completed",
        amount=1000.0,
        placed_at=NOW - timedelta(days=60),
        paid_at=NOW - timedelta(days=60),
        delivered_at=NOW - timedelta(days=59),
    )
    await _seed_message("acc-1", direction="inbound", received_at=NOW - timedelta(hours=1))
    await _seed_message("acc-1", direction="outbound", received_at=NOW - timedelta(hours=1))

    report = await AnalyticsService().report(account_id="acc-1", now=NOW)

    by_days = {window.days: window for window in report.windows}
    assert by_days[1].orders == 1
    assert by_days[1].amount == 10.0
    assert by_days[1].inbound_messages == 1
    assert by_days[1].outbound_messages == 1
    assert by_days[7].orders == 2
    assert by_days[7].amount == 100.0
    assert by_days[7].delivered_orders == 1
    assert by_days[30].orders == 2


async def test_report_is_scoped_to_one_account(analytics_db: None) -> None:
    await _seed_order(
        "acc-1",
        "O-1",
        status="paid",
        amount=10.0,
        placed_at=NOW - timedelta(hours=1),
        paid_at=NOW - timedelta(hours=1),
    )
    await _seed_order(
        "acc-2",
        "O-2",
        status="paid",
        amount=999.0,
        placed_at=NOW - timedelta(hours=1),
        paid_at=NOW - timedelta(hours=1),
    )

    one = await AnalyticsService().report(account_id="acc-1", windows=(1,), now=NOW)
    both = await AnalyticsService().report(windows=(1,), now=NOW)

    assert one.windows[0].amount == 10.0
    assert both.windows[0].amount == 1009.0


async def test_unknown_account_is_reported(analytics_db: None) -> None:
    with pytest.raises(ValueError, match="不存在"):
        await AnalyticsService().report(account_id="missing")


async def test_missing_paid_and_delivery_times_are_reported_without_invented_revenue(analytics_db):
    for account, order_id, status in [
        ("acc-1", "missing", "completed"),
        ("acc-2", "other", "delivered"),
        ("acc-1", "cancelled", "cancelled"),
    ]:
        await _seed_order(account, order_id, status=status, amount=100, placed_at=NOW, paid_at=None)
    report = await AnalyticsService().report(account_id="acc-1", now=NOW)
    assert report.orders_missing_paid_at == 1
    assert report.orders_missing_delivered_at == 1
    assert report.windows[0].amount == 0
    assert all(row.amount == 0 for row in report.revenue_daily)
    combined = await AnalyticsService().report(now=NOW)
    assert combined.orders_missing_paid_at == 2
    assert combined.orders_missing_delivered_at == 2


async def test_invalid_window_is_rejected(analytics_db: None) -> None:
    with pytest.raises(ValueError, match="1 到 365"):
        await AnalyticsService().report(windows=(0,))
    with pytest.raises(ValueError, match="至少需要"):
        await AnalyticsService().report(windows=())


async def test_automation_health_and_attention_count(analytics_db: None) -> None:
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        "request_flower",
        target_id="order-1",
        idempotency_key="request_flower:order-1",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="transport")

    report = await AnalyticsService().report(account_id="acc-1", now=NOW)

    assert report.automation.automation_uncertain == 1
    assert report.attention_items == 1


def test_cli_prints_the_report(analytics_db: None) -> None:
    recent = datetime.now(UTC) - timedelta(hours=1)

    async def seed() -> None:
        await _seed_order(
            "acc-1",
            "O-cli",
            status="paid",
            amount=12.5,
            placed_at=recent,
            paid_at=recent,
        )

    asyncio.run(seed())
    result = runner.invoke(app, ["analytics", "report", "-a", "acc-1", "--days", "1,7"])

    assert result.exit_code == 0, result.output
    assert "12.50" in result.output
    assert "镜像水位" in result.output


def test_cli_rejects_a_bad_window(analytics_db: None) -> None:
    result = runner.invoke(app, ["analytics", "report", "--days", "week"])

    assert result.exit_code == 1
    assert "只能是逗号分隔的天数" in result.output


async def test_daily_revenue_uses_business_dates_paid_statuses_and_zero_fills(
    analytics_db: None,
) -> None:
    entries = [
        ("before-start", "paid", 50, datetime(2026, 9, 14, 15, 59, tzinfo=UTC)),
        ("at-start", "paid", 1, datetime(2026, 9, 14, 16, tzinfo=UTC)),
        ("decimal-a", "paid", 0.1, datetime(2026, 9, 17, 1, tzinfo=UTC)),
        ("decimal-b", "paid", 0.2, datetime(2026, 9, 17, 2, tzinfo=UTC)),
        ("before-midnight", "delivered", 2, datetime(2026, 9, 20, 15, 59, tzinfo=UTC)),
        ("after-midnight", "completed", 3, datetime(2026, 9, 20, 16, tzinfo=UTC)),
        ("unpaid", "pending_payment", 100, NOW - timedelta(hours=1)),
        ("cancelled", "cancelled", 100, NOW - timedelta(hours=1)),
        ("future", "paid", 100, NOW + timedelta(minutes=1)),
    ]
    for order_id, status, amount, paid_at in entries:
        await _seed_order(
            "acc-1", order_id, status=status, amount=amount, placed_at=paid_at, paid_at=paid_at
        )
    await _seed_order(
        "acc-1", "missing-paid-time", status="paid", amount=100, placed_at=NOW, paid_at=None
    )
    await _seed_order("acc-2", "other-account", status="paid", amount=9, placed_at=NOW, paid_at=NOW)
    report = await AnalyticsService().report(account_id="acc-1", windows=(1, 7), now=NOW)
    series = report.revenue_daily
    assert [row.date.isoformat() for row in series] == [f"2026-09-{day}" for day in range(15, 22)]
    assert [row.amount for row in series] == [1, 0, 0.3, 0, 0, 2, 3]
    assert [row.paid_orders for row in series] == [1, 0, 2, 0, 0, 1, 1]
    global_report = await AnalyticsService().report(windows=(7,), now=NOW)
    assert global_report.revenue_daily[-1].amount == 12
    assert global_report.revenue_daily[-1].paid_orders == 2


async def test_empty_daily_revenue_and_naive_utc_now_are_supported(analytics_db: None) -> None:
    report = await AnalyticsService().report(
        account_id="acc-1", windows=(7, 30), now=NOW.replace(tzinfo=None)
    )
    assert len(report.revenue_daily) == 30
    assert report.revenue_daily[-1].date.isoformat() == "2026-09-21"
    assert all(row.amount == 0 and row.paid_orders == 0 for row in report.revenue_daily)


async def test_custom_range_includes_both_business_days_and_excludes_outside(
    analytics_db: None,
) -> None:
    start = datetime(2026, 9, 17, 16, tzinfo=UTC)
    stop = datetime(2026, 9, 19, 16, tzinfo=UTC)
    for order_id, instant, amount in [
        ("before", start - timedelta(microseconds=1), 100),
        ("first", start, 0.1),
        ("last", stop - timedelta(microseconds=1), 0.2),
        ("after", stop, 1000),
    ]:
        await _seed_order(
            "acc-1",
            order_id,
            status="delivered",
            amount=amount,
            placed_at=instant,
            paid_at=instant,
            delivered_at=instant,
        )
        await _seed_message("acc-1", direction="inbound", received_at=instant)
        await _seed_message("acc-1", direction="outbound", received_at=instant)
    await _seed_order("acc-2", "other", status="paid", amount=9, placed_at=start, paid_at=start)
    async with get_async_session() as session, session.begin():
        pk = await session.scalar(select(AccountRow.id).where(AccountRow.account_id == "acc-1"))
        for i, instant in enumerate([start - timedelta(microseconds=1), start, stop]):
            session.add(
                ItemRow(
                    account_id=pk,
                    item_id=f"range-{i}",
                    title="商品",
                    price=1,
                    first_seen_at=instant,
                )
            )
    report = await AnalyticsService().report(
        account_id="acc-1", now=NOW, start_date=date(2026, 9, 18), end_date=date(2026, 9, 19)
    )
    assert report.range_start == date(2026, 9, 18)
    assert report.range_end == date(2026, 9, 19)
    assert len(report.windows) == 1
    window = report.windows[0]
    assert (window.days, window.orders, window.paid_orders, window.delivered_orders) == (2, 2, 2, 2)
    assert window.amount == pytest.approx(0.3)
    assert (window.inbound_messages, window.outbound_messages, window.new_items) == (2, 2, 1)
    assert [row.date for row in report.revenue_daily] == [date(2026, 9, 18), date(2026, 9, 19)]
    assert [row.amount for row in report.revenue_daily] == [0.1, 0.2]
    one_day = await AnalyticsService().report(
        account_id="acc-1", now=NOW, start_date=date(2026, 9, 18), end_date=date(2026, 9, 18)
    )
    assert one_day.windows[0].paid_orders == 1
    assert len(one_day.revenue_daily) == 1
    combined = await AnalyticsService().report(
        now=NOW, start_date=date(2026, 9, 18), end_date=date(2026, 9, 19)
    )
    assert combined.windows[0].amount == pytest.approx(9.3)


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (date(2026, 9, 18), None, "同时"),
        (None, date(2026, 9, 19), "同时"),
        (date(2026, 9, 19), date(2026, 9, 18), "早于"),
        (date(2026, 9, 18), date(2026, 9, 22), "今天"),
        (date(2025, 9, 20), date(2026, 9, 20), "365"),
    ],
)
async def test_custom_range_rejects_invalid_dates(analytics_db, start, end, message):
    with pytest.raises(ValueError, match=message):
        await AnalyticsService().report(now=NOW, start_date=start, end_date=end)


async def test_custom_range_maximum_and_current_day_future_exclusion(analytics_db):
    await _seed_order(
        "acc-1",
        "future",
        status="paid",
        amount=100,
        placed_at=NOW + timedelta(hours=1),
        paid_at=NOW + timedelta(hours=1),
    )
    report = await AnalyticsService().report(
        account_id="acc-1",
        now=NOW.replace(tzinfo=None),
        start_date=date(2025, 9, 22),
        end_date=date(2026, 9, 21),
    )
    assert len(report.revenue_daily) == 365
    assert report.windows[0].orders == report.windows[0].paid_orders == 0
    assert report.windows[0].amount == 0


def test_cli_supports_custom_dates(analytics_db):
    result = runner.invoke(
        app, ["analytics", "report", "--start-date", "2026-01-01", "--end-date", "2026-01-02"]
    )
    assert result.exit_code == 0, result.output
    assert "自定义范围:2026-01-01 至 2026-01-02" in result.output
    missing = runner.invoke(app, ["analytics", "report", "--start-date", "2026-01-01"])
    assert missing.exit_code == 1
    assert "同时" in missing.output
    invalid = runner.invoke(app, ["analytics", "report", "--start-date", "invalid"])
    assert invalid.exit_code == 1
