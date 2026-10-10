"""高风险商品动作的编排:Policy → Dry Run → Guardrail → Action → Audit(M14)。

Dry Run 与执行分离:plan() 只读镜像与活动数据,产出可审阅的行动清单;apply() 只接受
plan() 的结果,所以代码里不存在"跳过 Dry Run 直接动手"的路径。真正调用闲鱼的执行器
由调用方注入——写入类协议尚未真实校准,默认没有执行器,apply() 会拒绝执行而不是假装成功。

每次判定与每次动作都会写入 audit_logs(action=item.lifecycle),没有审计的动作无法发生。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, AuditLog, Message, Order, ReplyLog, get_async_session
from xianyu_agent.domain.item.entity import Item
from xianyu_agent.domain.item.filter_policy import ItemFilter
from xianyu_agent.domain.item.lifecycle_policy import (
    OVERRIDE_EXCLUDE,
    LifecycleAction,
    LifecycleContext,
    LifecycleDecision,
    LifecyclePolicy,
    decide_with_override,
)

from .query_service import ItemQueryService

logger = logging.getLogger(__name__)

LIFECYCLE_ACTION = "item.lifecycle"
ORDER_ACTIVITY_STATUSES = ("pending_payment", "paid", "delivered", "completed")
DEFAULT_PLAN_LIMIT = 500

LifecycleExecutor = Callable[[str, Item, LifecycleAction], Awaitable[bool]]


def lifecycle_policy(
    *,
    delist_after_days: int | None = None,
    delete_after_days: int | None = None,
    daily_limit: int | None = None,
    enabled: bool = True,
) -> LifecyclePolicy:
    """Build an enabled policy from adapter input; 0/None means "not set"."""
    return LifecyclePolicy(
        enabled=enabled,
        delist_after_days=delist_after_days or None,
        delete_after_days=delete_after_days or None,
        daily_limit=daily_limit or None,
    )


def lifecycle_item_filter(
    *,
    include_keywords: Sequence[str] = (),
    exclude_keywords: Sequence[str] = (),
    min_price: float | str | None = None,
    max_price: float | str | None = None,
) -> ItemFilter:
    """Build the selection scope; falsy prices mean "no bound"."""
    return ItemFilter(
        include_keywords=tuple(include_keywords),
        exclude_keywords=tuple(exclude_keywords),
        min_price=_money(min_price),
        max_price=_money(max_price),
    )


def _money(value: float | str | None) -> Decimal | None:
    if value in (None, "", 0, 0.0):
        return None
    return Decimal(str(value))


@dataclass(frozen=True)
class LifecyclePlanRow:
    """One item with the action the policy selected for it."""

    item: Item
    decision: LifecycleDecision


@dataclass(frozen=True)
class LifecyclePlan:
    """Dry-run result: nothing has been changed yet."""

    account_id: str
    rows: tuple[LifecyclePlanRow, ...]
    generated_at: datetime
    # 扫描范围必须是显式的:超过 limit 时宁可告诉运营"还有更多",也不能静默只看一部分。
    scanned: int = 0
    truncated: bool = False

    @property
    def actionable(self) -> tuple[LifecyclePlanRow, ...]:
        return tuple(row for row in self.rows if row.decision.actionable)

    @property
    def counts(self) -> dict[str, int]:
        counts = {"KEEP": 0, "DELIST": 0, "DELETE": 0}
        for row in self.rows:
            counts[row.decision.action] += 1
        return counts


@dataclass(frozen=True)
class LifecycleActionResult:
    """Outcome of one item action."""

    item_id: str
    action: str
    executed: bool
    error: str | None = None


@dataclass(frozen=True)
class LifecycleRunResult:
    """Outcome of applying one plan."""

    results: tuple[LifecycleActionResult, ...]

    @property
    def executed(self) -> int:
        return sum(1 for row in self.results if row.executed)

    @property
    def failed(self) -> int:
        return sum(1 for row in self.results if not row.executed)


class ItemLifecycleService:
    """Plan and execute delist/delete actions with an audit trail."""

    def __init__(
        self,
        *,
        executor: LifecycleExecutor | None = None,
        query_service: ItemQueryService | None = None,
    ) -> None:
        self._executor = executor
        self._query = query_service or ItemQueryService()

    async def plan(
        self,
        account_id: str,
        *,
        policy: LifecyclePolicy,
        item_filter: ItemFilter | None = None,
        limit: int = DEFAULT_PLAN_LIMIT,
        now: datetime | None = None,
    ) -> LifecyclePlan:
        """Read the mirror and decide, without touching the platform."""
        observed_at = now or datetime.now(UTC)
        scope = item_filter or ItemFilter()
        # 多取一行就能判断"还有没有"。不这么做的话,计划看起来完整,实际只覆盖了前 limit 件。
        fetched = await self._query.list_items(
            account_id, on_sale_only=scope.on_sale_only, limit=limit + 1
        )
        truncated = len(fetched) > limit
        items = fetched[:limit]
        # 人工 exclude 的商品彻底不进判定:它们连 Dry Run 都不该出现,否则运营会反复看到噪声。
        scoped = [item for item in items if scope.matches(item)]
        selected = [
            item for item in scoped if item.automation_override != OVERRIDE_EXCLUDE
        ]
        excluded = len(scoped) - len(selected)
        account_pk = await _account_pk(account_id)
        action_count = await _daily_action_count(account_id, observed_at)
        rows: list[LifecyclePlanRow] = []
        for item in selected:
            context = LifecycleContext(
                now=observed_at,
                listed_at=item.listed_at,
                last_reply_at=(
                    await _last_reply_at(account_pk, item.external_item_id)
                    if account_pk is not None
                    else None
                ),
                last_order_at=(
                    await _last_order_at(account_pk, item.external_item_id)
                    if account_pk is not None
                    else None
                ),
                daily_action_count=action_count,
            )
            decision = decide_with_override(
                policy, context, override=item.automation_override
            )
            if decision is None:  # pragma: no cover - 已在上面过滤,这里只是兜底
                continue
            rows.append(LifecyclePlanRow(item=item, decision=decision))
        plan = LifecyclePlan(
            account_id=account_id,
            rows=tuple(rows),
            generated_at=observed_at,
            scanned=len(items),
            truncated=truncated,
        )
        await write_lifecycle_audit(
            account_id,
            item_id=None,
            action="DRY_RUN",
            reason=(
                f"计划 {len(rows)} 项,可执行 {len(plan.actionable)} 项,"
                f"人工排除 {excluded} 项"
                + (f",仅扫描前 {plan.scanned} 件(还有更多未纳入)" if truncated else "")
            ),
            result="dry_run",
        )
        return plan

    async def apply(
        self,
        plan: LifecyclePlan,
        *,
        allow_delete: bool = False,
    ) -> LifecycleRunResult:
        """Execute an approved plan through the injected executor.

        Delete is refused unless the caller opts in explicitly, and an
        unconfigured executor is a hard stop: a missing protocol implementation
        must never look like a successful action.
        """
        if self._executor is None:
            msg = "未配置商品动作执行器,拒绝执行"
            raise RuntimeError(msg)
        results: list[LifecycleActionResult] = []
        for row in plan.actionable:
            item_id = row.item.external_item_id
            action = row.decision.action
            if action == "DELETE" and not allow_delete:
                results.append(
                    LifecycleActionResult(
                        item_id=item_id, action=action, executed=False, error="未开启删除动作"
                    )
                )
                await write_lifecycle_audit(
                    plan.account_id,
                    item_id=item_id,
                    action=action,
                    reason=row.decision.reason,
                    result="blocked",
                    error="未开启删除动作",
                )
                continue
            try:
                ok = await self._executor(plan.account_id, row.item, action)
            except Exception as exc:
                logger.warning("item lifecycle action failed type=%s", type(exc).__name__)
                ok = False
                error: str | None = f"{type(exc).__name__}"
            else:
                error = None if ok else "执行器返回失败"
            results.append(
                LifecycleActionResult(item_id=item_id, action=action, executed=ok, error=error)
            )
            await write_lifecycle_audit(
                plan.account_id,
                item_id=item_id,
                action=action,
                reason=row.decision.reason,
                result="executed" if ok else "failed",
                error=error,
            )
        return LifecycleRunResult(results=tuple(results))


async def write_lifecycle_audit(
    account_id: str,
    *,
    item_id: str | None,
    action: str,
    reason: str,
    result: str,
    error: str | None = None,
) -> None:
    """Append one lifecycle audit row; never raises into the caller."""
    try:
        async with get_async_session() as session:
            session.add(
                AuditLog(
                    actor="system",
                    action=LIFECYCLE_ACTION,
                    target=account_id,
                    params={"item_id": item_id, "decision": action, "reason": reason},
                    result=result,
                    error=error,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("item lifecycle audit write failed: %s", type(exc).__name__)


async def recent_lifecycle_events(*, limit: int = 20) -> Sequence[AuditLog]:
    """Read recent lifecycle audit rows, newest first."""
    async with get_async_session() as session:
        stmt = (
            select(AuditLog)
            .where(AuditLog.action == LIFECYCLE_ACTION)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


async def _account_pk(account_id: str) -> int | None:
    async with get_async_session() as session:
        row = (
            await session.execute(
                select(Account.id).where(Account.account_id == account_id).limit(1)
            )
        ).scalar_one_or_none()
        return int(row) if row is not None else None


async def _daily_action_count(account_id: str, now: datetime) -> int:
    day_start = _local_day_start(now)
    async with get_async_session() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == LIFECYCLE_ACTION)
                    .where(AuditLog.target == account_id)
                    .where(AuditLog.result == "executed")
                    .where(AuditLog.created_at >= day_start)
                )
            ).scalar_one()
            or 0
        )


def _local_day_start(now: datetime) -> datetime:
    """Start of today in the configured business timezone, returned as UTC."""
    timezone = ZoneInfo(get_settings().tz)
    local = _as_utc(now).astimezone(timezone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def _last_reply_at(account_pk: int, item_id: str) -> datetime | None:
    async with get_async_session() as session:
        return (
            await session.execute(
                select(func.max(ReplyLog.sent_at))
                .join(Message, ReplyLog.message_id == Message.id)
                .where(Message.account_id == account_pk)
                .where(Message.item_id == item_id)
            )
        ).scalar_one_or_none()


async def _last_order_at(account_pk: int, item_id: str) -> datetime | None:
    async with get_async_session() as session:
        return (
            await session.execute(
                select(func.max(func.coalesce(Order.paid_at, Order.placed_at, Order.created_at)))
                .where(Order.account_id == account_pk)
                .where(Order.item_id == item_id)
                .where(Order.status.in_(ORDER_ACTIVITY_STATUSES))
            )
        ).scalar_one_or_none()


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
