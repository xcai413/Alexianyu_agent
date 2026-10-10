"""Durable, calibration-gated scheduling for platform automation actions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select

from xianyu_agent.application.pacing import AccountPacer, get_pacer
from xianyu_agent.db import Account, Item, Order, get_async_session
from xianyu_agent.infrastructure.database.models.enums import OrderStatus
from xianyu_agent.infrastructure.database.scheduler_queue import SqlAlchemyDueQueue
from xianyu_agent.protocol.mtop.endpoints.buyer_flower import XianyuBuyerFlowerClient
from xianyu_agent.protocol.mtop.endpoints.item_polish import XianyuItemPolishClient
from xianyu_agent.runtime.scheduler import (
    Clock,
    LeasedWork,
    RunResult,
    SchedulerRecovery,
    SchedulerRunner,
    SystemClock,
)

from .platform_service import (
    ACTION_POLISH,
    ACTION_REQUEST_FLOWER,
    PlatformAutomationService,
    PlatformAutomationView,
)

WORK_PREFIX = "platform-automation:"
WORK_KIND = "platform_automation"


class PolishClient(Protocol):
    async def polish_item(self, account_id: str, item_id: str) -> Any: ...


class FlowerClient(Protocol):
    async def request_flower(self, account_id: str, order_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class PlatformSchedulerResult:
    enabled: bool
    seeded: int
    recovered: int
    runner: RunResult


class PlatformAutomationScheduler:
    """Seed and execute daily work only after both automation gates open."""

    def __init__(
        self,
        *,
        instance_id: str,
        active: bool,
        timezone: str = "Asia/Shanghai",
        service: PlatformAutomationService | None = None,
        queue: SqlAlchemyDueQueue | None = None,
        clock: Clock | None = None,
        pacer: AccountPacer | None = None,
        polish_client: PolishClient | None = None,
        flower_client: FlowerClient | None = None,
    ) -> None:
        self._active = active
        self._timezone = ZoneInfo(timezone)
        self._service = service or PlatformAutomationService()
        self._queue = queue or SqlAlchemyDueQueue(work_id_prefix=WORK_PREFIX)
        self._clock = clock or SystemClock()
        self._pacer = pacer or get_pacer()
        self._polish = polish_client or XianyuItemPolishClient()
        self._flower = flower_client or XianyuBuyerFlowerClient()
        self._recovery = SchedulerRecovery(self._queue, self._clock)
        self._runner = SchedulerRunner(
            self._queue,
            self._handle,
            self._clock,
            lease_owner=instance_id,
            batch_size=10,
            concurrency=1,
        )
        self._recovered = False

    async def run_once(self) -> PlatformSchedulerResult:
        if not self._active:
            return PlatformSchedulerResult(False, 0, 0, RunResult(0, 0, 0, 0))
        recovered = 0
        if not self._recovered:
            recovered = await self._recovery.recover_once()
            self._recovered = True
        seeded = await self._seed()
        return PlatformSchedulerResult(True, seeded, recovered, await self._runner.run_once())

    async def _seed(self) -> int:
        now = self._clock.now().astimezone(UTC)
        seeded = 0
        for config in await self._service.list_executable():
            available_at = next_occurrence(
                now,
                schedule_time=config.schedule_time,
                timezone=self._timezone,
                quiet_hours=config.quiet_hours,
            )
            occurrence = available_at.astimezone(self._timezone).strftime("%Y%m%dT%H%M")
            work_id = automation_work_id(config.account_id, config.action, occurrence)
            created = await self._queue.enqueue_once(
                work_id=work_id,
                payload={
                    "kind": WORK_KIND,
                    "account_id": config.account_id,
                    "action": config.action,
                    "occurrence": occurrence,
                },
                available_at=available_at,
            )
            seeded += int(created)
        return seeded

    async def _handle(self, work: LeasedWork) -> None:
        if not self._active or work.payload.get("kind") != WORK_KIND:
            raise ValueError("unsupported scheduler work")
        account_id = work.payload.get("account_id")
        action = work.payload.get("action")
        if not isinstance(account_id, str) or not isinstance(action, str):
            raise TypeError("platform automation work payload is incomplete")
        config = await self._service.get(account_id, action)
        if config is None or not config.enabled or not config.calibrated:
            return
        if _is_quiet(self._clock.now(), self._timezone, config.quiet_hours):
            return
        if action == ACTION_POLISH:
            await self._run_polish(config)
        elif action == ACTION_REQUEST_FLOWER:
            await self._run_flower(config)
        else:  # pragma: no cover - service validation owns the action set
            raise ValueError(f"unsupported platform automation action: {action}")

    async def _run_polish(self, config: PlatformAutomationView) -> None:
        for item_id in await self._on_sale_items(config.account_id):
            run_id = await self._service.reserve_run(
                config.account_id,
                ACTION_POLISH,
                target_id=item_id,
                idempotency_key=f"polish:{item_id}:{self._local_date().isoformat()}",
            )
            if run_id is None:
                continue
            if not await self._service.is_executable(config.account_id, ACTION_POLISH):
                await self._service.finish_run(run_id, status="skipped", result="gate_closed")
                return
            await self._pacer.acquire(config.account_id)
            try:
                result = await self._polish.polish_item(config.account_id, item_id)
            except Exception as exc:
                # 平台可能已经收到擦亮请求但响应丢失;结果不明只能冻结,不能自动重发。
                await self._service.finish_run(
                    run_id,
                    status="uncertain",
                    result=type(exc).__name__,
                )
            else:
                outcome = (
                    "already_polished"
                    if bool(getattr(result, "already_polished", False))
                    else "polished"
                )
                await self._service.finish_run(
                    run_id, status="completed", result=outcome
                )

    async def _run_flower(self, config: PlatformAutomationView) -> None:
        for order_id in await self._eligible_orders(config):
            run_id = await self._service.reserve_run(
                config.account_id,
                ACTION_REQUEST_FLOWER,
                target_id=order_id,
                idempotency_key=f"request_flower:{order_id}",
            )
            if run_id is None:
                continue
            if not await self._service.is_executable(
                config.account_id, ACTION_REQUEST_FLOWER
            ):
                await self._service.finish_run(run_id, status="skipped", result="gate_closed")
                return
            await self._pacer.acquire(config.account_id)
            try:
                await self._flower.request_flower(config.account_id, order_id)
            except Exception as exc:
                # The only reference does not expose a reliable dispatched signal.
                # Never retry an uncertain buyer-facing prompt automatically.
                await self._service.finish_run(
                    run_id,
                    status="uncertain",
                    result=type(exc).__name__,
                )
            else:
                await self._service.finish_run(
                    run_id, status="completed", result="requested"
                )

    async def _on_sale_items(self, account_id: str) -> list[str]:
        async with get_async_session() as session:
            rows = await session.scalars(
                select(Item.item_id)
                .join(Account, Account.id == Item.account_id)
                .where(
                    Account.account_id == account_id,
                    Item.is_on_sale.is_(True),
                )
                .order_by(Item.item_id)
            )
            return list(rows.all())

    async def _eligible_orders(self, config: PlatformAutomationView) -> list[str]:
        if config.calibration_approved_at is None:  # pragma: no cover - gated caller
            return []
        approved_at = _as_utc(config.calibration_approved_at)
        cutoff = self._clock.now().astimezone(UTC) - timedelta(
            minutes=config.delay_minutes
        )
        async with get_async_session() as session:
            rows = await session.scalars(
                select(Order.order_id)
                .join(Account, Account.id == Order.account_id)
                .where(
                    Account.account_id == config.account_id,
                    Order.status.in_(
                        (OrderStatus.DELIVERED.value, OrderStatus.COMPLETED.value)
                    ),
                    Order.delivered_at.is_not(None),
                    Order.delivered_at >= approved_at,
                    Order.delivered_at <= cutoff,
                )
                .order_by(Order.delivered_at, Order.order_id)
            )
            return list(rows.all())

    def _local_date(self) -> date:
        return self._clock.now().astimezone(self._timezone).date()


def next_occurrence(
    now: datetime,
    *,
    schedule_time: str,
    timezone: ZoneInfo,
    quiet_hours: str | None,
) -> datetime:
    """Return the next configured local occurrence, shifted out of quiet hours."""
    local_now = _as_utc(now).astimezone(timezone)
    hour, minute = (int(part) for part in schedule_time.split(":"))
    candidate = datetime.combine(
        local_now.date(), time(hour, minute), tzinfo=timezone
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    candidate = _move_out_of_quiet(candidate, quiet_hours)
    return candidate.astimezone(UTC)


def automation_work_id(account_id: str, action: str, occurrence: str) -> str:
    """Build a deterministic queue key that fits scheduler_work.work_id(64)."""
    business_key = f"{account_id}:{action}:{occurrence}"
    digest = hashlib.sha256(business_key.encode()).hexdigest()[:40]
    return f"{WORK_PREFIX}{digest}"


async def enqueue_immediate_run(
    *,
    account_id: str,
    action: str,
    active: bool,
    timezone: str = "Asia/Shanghai",
    service: PlatformAutomationService | None = None,
    queue: SqlAlchemyDueQueue | None = None,
    clock: Clock | None = None,
) -> str:
    """把一次平台动作立刻排进队列,返回任务键。

    这是"现在就做一次"的入口,但它不绕过任何门禁:全局必须是 active,动作必须已启用且已登记真实
    校准,并且当前不在静默时段。执行仍然由 daemon 的同一轮调度完成,因此幂等与租约语义与定时任务
    完全一致——不存在第二条执行路径。
    """
    if not active:
        msg = (
            "当前 XIANYU_AUTOMATION_MODE 不是 active,不会执行任何真实平台动作;"
            "确认账号与动作已校准并启用后,再显式改为 active"
        )
        raise ValueError(msg)
    resolved_service = service or PlatformAutomationService()
    config = await resolved_service.get(account_id, action)
    if config is None:
        msg = f"账号 {account_id} 还没有 {action} 的自动化配置"
        raise ValueError(msg)
    if not config.calibrated:
        msg = f"{action} 尚未登记真实校准,先按待真实验证清单执行并登记批准"
        raise ValueError(msg)
    if not config.enabled:
        msg = f"{action} 当前是停用状态,先启用再立即执行"
        raise ValueError(msg)
    zone = ZoneInfo(timezone)
    resolved_clock = clock or SystemClock()
    now = resolved_clock.now().astimezone(UTC)
    if _is_quiet(now, zone, config.quiet_hours):
        msg = f"当前处于静默时段 {config.quiet_hours},平台动作不会执行"
        raise ValueError(msg)
    stamp = now.astimezone(zone).strftime("%Y%m%dT%H%M%S")
    work_id = automation_work_id(account_id, action, f"manual-{stamp}")
    due_queue = queue or SqlAlchemyDueQueue(work_id_prefix=WORK_PREFIX)
    await due_queue.enqueue_once(
        work_id=work_id,
        payload={
            "kind": WORK_KIND,
            "account_id": account_id,
            "action": action,
            "occurrence": stamp,
            "manual": True,
        },
        available_at=now,
    )
    return work_id


def _is_quiet(now: datetime, timezone: ZoneInfo, quiet_hours: str | None) -> bool:
    if not quiet_hours:
        return False
    local = _as_utc(now).astimezone(timezone)
    start, end = (_minute_of_day(value) for value in quiet_hours.split("-"))
    current = local.hour * 60 + local.minute
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _move_out_of_quiet(candidate: datetime, quiet_hours: str | None) -> datetime:
    if not quiet_hours or not _is_quiet(candidate, ZoneInfo(str(candidate.tzinfo)), quiet_hours):
        return candidate
    start, end = (_minute_of_day(value) for value in quiet_hours.split("-"))
    end_time = time(end // 60, end % 60)
    current = candidate.hour * 60 + candidate.minute
    next_day = start > end and current >= start
    target_date = candidate.date() + timedelta(days=int(next_day))
    return datetime.combine(target_date, end_time, tzinfo=candidate.tzinfo)


def _minute_of_day(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
