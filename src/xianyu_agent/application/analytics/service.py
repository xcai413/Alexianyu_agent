"""运营分析(对照 Helper 的 /analytics/* 与 butler 的仪表盘)。

只读聚合,不产生任何平台动作。指标全部来自本地镜像,因此"数字对得上"的前提是镜像同步正常:
报告里同时给出镜像水位(在售商品数、最近同步时间),避免把"还没同步"误读成"没有生意"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from xianyu_agent.application.attention import AttentionService
from xianyu_agent.db import (
    Account,
    Item,
    Message,
    Order,
    PlatformAutomationRun,
    get_async_session,
)
from xianyu_agent.domain.delivery import DeliveryJobState
from xianyu_agent.infrastructure.database.models import DeliveryJob as DeliveryJobRow
from xianyu_agent.utils.time_utils import DEFAULT_BUSINESS_TIMEZONE, to_local

DEFAULT_WINDOWS: tuple[int, ...] = (1, 7, 30)
_PAID_STATUSES = ("paid", "delivered", "completed")


@dataclass(frozen=True, slots=True)
class AnalyticsWindow:
    """一个时间窗口内的经营数字。"""

    days: int
    orders: int = 0
    paid_orders: int = 0
    delivered_orders: int = 0
    amount: float = 0.0
    inbound_messages: int = 0
    outbound_messages: int = 0
    new_items: int = 0


@dataclass(frozen=True, slots=True)
class DailyRevenue:
    """业务日内已付款订单金额,不代表扣除成本与退款后的利润。"""

    date: date
    amount: float = 0.0
    paid_orders: int = 0


@dataclass(frozen=True, slots=True)
class AutomationHealth:
    """自动化执行健康状况:哪些已经完成、哪些卡住等人。"""

    jobs_completed: int = 0
    jobs_failed: int = 0
    jobs_waiting_human: int = 0
    automation_completed: int = 0
    automation_uncertain: int = 0


@dataclass(frozen=True, slots=True)
class AnalyticsReport:
    """一份完整报告:窗口指标 + 自动化健康 + 镜像水位。"""

    generated_at: datetime
    windows: tuple[AnalyticsWindow, ...] = ()
    automation: AutomationHealth = field(default_factory=AutomationHealth)
    on_sale_items: int = 0
    attention_items: int = 0
    last_item_sync_at: datetime | None = None
    last_order_sync_at: datetime | None = None
    revenue_daily: tuple[DailyRevenue, ...] = ()
    range_start: date | None = None
    range_end: date | None = None
    orders_missing_paid_at: int = 0
    orders_missing_delivered_at: int = 0


class AnalyticsService:
    """Build one read-only analytics report from the local mirrors."""

    async def report(
        self,
        *,
        account_id: str | None = None,
        windows: tuple[int, ...] = DEFAULT_WINDOWS,
        now: datetime | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AnalyticsReport:
        if not windows:
            msg = "至少需要一个统计窗口"
            raise ValueError(msg)
        if any(days < 1 or days > 365 for days in windows):
            msg = "统计窗口必须在 1 到 365 天之间"
            raise ValueError(msg)
        observed_at = now or datetime.now(UTC)
        local_now = to_local(observed_at)
        assert local_now is not None
        range_days = None
        since = until = None
        if start_date is not None or end_date is not None:
            if start_date is None or end_date is None:
                raise ValueError("自定义范围需要同时填写开始和结束日期")
            range_days = (end_date - start_date).days + 1
            if range_days < 1:
                raise ValueError("结束日期不能早于开始日期")
            if range_days > 365:
                raise ValueError("自定义范围最多 365 天")
            if end_date > local_now.date():
                raise ValueError("结束日期不能晚于今天")
            since = datetime.combine(
                start_date, datetime.min.time(), tzinfo=DEFAULT_BUSINESS_TIMEZONE
            ).astimezone(UTC)
            until = min(
                datetime.combine(
                    end_date + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=DEFAULT_BUSINESS_TIMEZONE,
                ).astimezone(UTC),
                local_now.astimezone(UTC) + timedelta(microseconds=1),
            )
        account_pk = await self._account_pk(account_id)
        if account_id is not None and account_pk is None:
            msg = f"账号 {account_id} 不存在"
            raise ValueError(msg)
        async with get_async_session() as session:
            built = [
                await self._window(session, account_pk, days, observed_at, since=since, until=until)
                for days in ((range_days,) if range_days is not None else sorted(set(windows)))
            ]
            revenue_daily = await self._daily_revenue(
                session,
                account_pk,
                range_days or max(windows),
                observed_at,
                start_date=start_date,
            )
            automation = await self._automation_health(session, account_pk)
            missing_paid = await self._count(
                session,
                select(func.count(Order.id)).where(
                    Order.status.in_(_PAID_STATUSES), Order.paid_at.is_(None)
                ),
                Order.account_id,
                account_pk,
            )
            missing_delivered = await self._count(
                session,
                select(func.count(Order.id)).where(
                    Order.status.in_(("delivered", "completed")), Order.delivered_at.is_(None)
                ),
                Order.account_id,
                account_pk,
            )
            on_sale = await self._count(
                session,
                select(func.count(Item.id)).where(Item.is_on_sale.is_(True)),
                Item.account_id,
                account_pk,
            )
            last_item_sync = await session.scalar(
                self._scoped(select(func.max(Item.last_synced_at)), Item.account_id, account_pk)
            )
            last_order_sync = await session.scalar(
                self._scoped(select(func.max(Order.updated_at)), Order.account_id, account_pk)
            )
        attention = await AttentionService().list_items(account_id=account_id, limit=1000)
        return AnalyticsReport(
            generated_at=observed_at,
            windows=tuple(built),
            automation=automation,
            on_sale_items=int(on_sale),
            attention_items=len(attention),
            last_item_sync_at=last_item_sync,
            last_order_sync_at=last_order_sync,
            revenue_daily=revenue_daily,
            range_start=start_date,
            range_end=end_date,
            orders_missing_paid_at=missing_paid,
            orders_missing_delivered_at=missing_delivered,
        )

    async def _daily_revenue(
        self,
        session,
        account_pk: int | None,
        days: int,
        now: datetime,
        *,
        start_date: date | None = None,
    ) -> tuple[DailyRevenue, ...]:
        local_now = to_local(now)
        assert local_now is not None
        first_day = start_date or local_now.date() - timedelta(days=days - 1)
        start = datetime.combine(first_day, datetime.min.time(), tzinfo=DEFAULT_BUSINESS_TIMEZONE)
        end = start + timedelta(days=days)
        dates = tuple(first_day + timedelta(days=offset) for offset in range(days))
        amounts: dict[date, Decimal] = {}
        counts: dict[date, int] = {}
        # 仅读取时间与金额并逐行聚合,不把订单或买家资料带入报表。
        result = await session.stream(
            self._scoped(
                select(Order.paid_at, Order.amount).where(
                    Order.paid_at >= start.astimezone(UTC),
                    Order.paid_at < end.astimezone(UTC),
                    Order.paid_at <= local_now.astimezone(UTC),
                    Order.status.in_(_PAID_STATUSES),
                ),
                Order.account_id,
                account_pk,
            )
        )
        async for paid_at, amount in result:
            paid_local = to_local(paid_at)
            assert paid_local is not None
            day = paid_local.date()
            amounts[day] = amounts.get(day, Decimal(0)) + Decimal(str(amount))
            counts[day] = counts.get(day, 0) + 1
        return tuple(
            DailyRevenue(
                date=day,
                amount=float(amounts.get(day, Decimal(0)).quantize(Decimal("0.01"))),
                paid_orders=counts.get(day, 0),
            )
            for day in dates
        )

    async def _window(
        self,
        session,
        account_pk: int | None,
        days: int,
        now: datetime,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AnalyticsWindow:
        since = since if since is not None else now - timedelta(days=days)

        def in_range(column):
            return (column >= since,) if until is None else (column >= since, column < until)

        orders = await self._count(
            session,
            select(func.count(Order.id)).where(*in_range(Order.placed_at)),
            Order.account_id,
            account_pk,
        )
        paid_orders = await self._count(
            session,
            select(func.count(Order.id)).where(
                *in_range(Order.paid_at), Order.status.in_(_PAID_STATUSES)
            ),
            Order.account_id,
            account_pk,
        )
        delivered_orders = await self._count(
            session,
            select(func.count(Order.id)).where(*in_range(Order.delivered_at)),
            Order.account_id,
            account_pk,
        )
        amount = await session.scalar(
            self._scoped(
                select(func.coalesce(func.sum(Order.amount), 0.0)).where(
                    *in_range(Order.paid_at), Order.status.in_(_PAID_STATUSES)
                ),
                Order.account_id,
                account_pk,
            )
        )
        inbound = await self._count(
            session,
            select(func.count(Message.id)).where(
                *in_range(Message.received_at), Message.direction == "inbound"
            ),
            Message.account_id,
            account_pk,
        )
        outbound = await self._count(
            session,
            select(func.count(Message.id)).where(
                *in_range(Message.received_at), Message.direction == "outbound"
            ),
            Message.account_id,
            account_pk,
        )
        new_items = await self._count(
            session,
            select(func.count(Item.id)).where(*in_range(Item.first_seen_at)),
            Item.account_id,
            account_pk,
        )
        return AnalyticsWindow(
            days=days,
            orders=int(orders),
            paid_orders=int(paid_orders),
            delivered_orders=int(delivered_orders),
            amount=float(amount or 0.0),
            inbound_messages=int(inbound),
            outbound_messages=int(outbound),
            new_items=int(new_items),
        )

    async def _automation_health(self, session, account_pk: int | None) -> AutomationHealth:
        human_states = (
            DeliveryJobState.UNCERTAIN.value,
            DeliveryJobState.NEEDS_CONFIRMATION.value,
            DeliveryJobState.BLOCKED.value,
        )
        completed = await self._count(
            session,
            select(func.count(DeliveryJobRow.id)).where(
                DeliveryJobRow.state == DeliveryJobState.COMPLETED.value
            ),
            DeliveryJobRow.account_id,
            account_pk,
        )
        failed = await self._count(
            session,
            select(func.count(DeliveryJobRow.id)).where(
                DeliveryJobRow.state == DeliveryJobState.FAILED_FINAL.value
            ),
            DeliveryJobRow.account_id,
            account_pk,
        )
        waiting = await self._count(
            session,
            select(func.count(DeliveryJobRow.id)).where(DeliveryJobRow.state.in_(human_states)),
            DeliveryJobRow.account_id,
            account_pk,
        )
        automation_completed = await session.scalar(
            self._scoped(
                select(func.count(PlatformAutomationRun.id)).where(
                    PlatformAutomationRun.status == "completed"
                ),
                PlatformAutomationRun.account_id,
                account_pk,
            )
        )
        automation_uncertain = await session.scalar(
            self._scoped(
                select(func.count(PlatformAutomationRun.id)).where(
                    PlatformAutomationRun.status == "uncertain"
                ),
                PlatformAutomationRun.account_id,
                account_pk,
            )
        )
        return AutomationHealth(
            jobs_completed=int(completed),
            jobs_failed=int(failed),
            jobs_waiting_human=int(waiting),
            automation_completed=int(automation_completed or 0),
            automation_uncertain=int(automation_uncertain or 0),
        )

    async def _count(self, session, statement, account_column, account_pk: int | None) -> int:
        value = await session.scalar(self._scoped(statement, account_column, account_pk))
        return int(value or 0)

    @staticmethod
    def _scoped(statement, account_column, account_pk: int | None):
        return statement if account_pk is None else statement.where(account_column == account_pk)

    @staticmethod
    async def _account_pk(account_id: str | None) -> int | None:
        if account_id is None:
            return None
        async with get_async_session() as session:
            row = await session.scalar(
                select(Account).where(Account.account_id == account_id).limit(1)
            )
        return None if row is None else int(row.id)
