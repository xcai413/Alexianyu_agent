"""生命周期策略与动作判定(M14 的 Policy 与 Dry Run 判定)。

动作集合固定为 KEEP / DELIST / DELETE。默认不启用、也永不删除:删除不可逆,
必须由运营方显式配置删除阈值才可能被判为 DELETE。

保护规则先于动作判定生效:最近的回复活动或订单活动都会把商品留在 KEEP,避免把
正在沟通或正在成交的商品下架。时间缺失时同样保持 KEEP——缺数据不是动手的理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

LifecycleAction = Literal["KEEP", "DELIST", "DELETE"]
KEEP: LifecycleAction = "KEEP"

# 商品级人工覆盖:exclude 让商品彻底不进判定,keep 让它在判定里永远是 KEEP。
OVERRIDE_EXCLUDE = "exclude"
OVERRIDE_KEEP = "keep"
OVERRIDE_MODES: tuple[str, ...] = (OVERRIDE_EXCLUDE, OVERRIDE_KEEP)


@dataclass(frozen=True)
class LifecycleDecision:
    """What should happen to one item, with the reason for the audit trail."""

    action: LifecycleAction
    reason: str

    @property
    def actionable(self) -> bool:
        return self.action != KEEP


@dataclass(frozen=True)
class LifecyclePolicy:
    """Operator settings for automated delist/delete."""

    enabled: bool = False
    delist_after_days: int | None = None
    delete_after_days: int | None = None
    reply_protection_days: int = 7
    order_protection_days: int = 7
    daily_limit: int | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.delist_after_days, "delist_after_days"),
            (self.delete_after_days, "delete_after_days"),
        ):
            if value is not None and value < 0:
                msg = f"{name} 不能为负数"
                raise ValueError(msg)
        if self.reply_protection_days < 0 or self.order_protection_days < 0:
            msg = "保护窗口不能为负数"
            raise ValueError(msg)
        if self.daily_limit is not None and self.daily_limit < 1:
            msg = "daily_limit 至少为 1"
            raise ValueError(msg)
        if (
            self.delist_after_days is not None
            and self.delete_after_days is not None
            and self.delete_after_days < self.delist_after_days
        ):
            msg = "delete_after_days 不能早于 delist_after_days"
            raise ValueError(msg)


@dataclass(frozen=True)
class LifecycleContext:
    """Observed activity needed to judge one item."""

    now: datetime
    listed_at: datetime | None = None
    last_reply_at: datetime | None = None
    last_order_at: datetime | None = None
    daily_action_count: int = 0


def decide(policy: LifecyclePolicy, context: LifecycleContext) -> LifecycleDecision:
    """Judge one item; never returns an unconfigured action."""
    if not policy.enabled:
        return LifecycleDecision(KEEP, "策略未启用")
    return _guarded(policy, context)


def decide_with_override(
    policy: LifecyclePolicy,
    context: LifecycleContext,
    *,
    override: str | None = None,
) -> LifecycleDecision | None:
    """判断一件商品;返回 None 表示它被人工排除,不应出现在计划里。

    人工覆盖先于策略:``exclude`` 让商品完全不进判定,``keep`` 让它永远是 KEEP。
    运营的显式决定不该被自动化规则覆盖掉。
    """
    if override == OVERRIDE_EXCLUDE:
        return None
    if override == OVERRIDE_KEEP:
        return LifecycleDecision(KEEP, "人工锁定:已标记为不参与自动动作")
    return decide(policy, context)


def _guarded(policy: LifecyclePolicy, context: LifecycleContext) -> LifecycleDecision:
    """Apply protections first, then age thresholds, then the daily limit."""
    protection = _protection(policy, context)
    if protection is not None:
        return protection
    age_days = _age_days(context.listed_at, context.now)
    if age_days is None:
        return LifecycleDecision(KEEP, "缺少上架时间")
    candidate = _candidate(policy, age_days)
    if candidate.action == KEEP or _within_daily_limit(policy, context):
        return candidate
    return LifecycleDecision(KEEP, f"当日动作已达上限({policy.daily_limit})")


def _protection(policy: LifecyclePolicy, context: LifecycleContext) -> LifecycleDecision | None:
    """Return a KEEP decision while recent activity protects the item."""
    if _within(context.last_reply_at, context.now, policy.reply_protection_days):
        return LifecycleDecision(KEEP, f"回复活动保护({policy.reply_protection_days} 天)")
    if _within(context.last_order_at, context.now, policy.order_protection_days):
        return LifecycleDecision(KEEP, f"订单活动保护({policy.order_protection_days} 天)")
    return None


def _within_daily_limit(policy: LifecyclePolicy, context: LifecycleContext) -> bool:
    return policy.daily_limit is None or context.daily_action_count < policy.daily_limit


def _candidate(policy: LifecyclePolicy, age_days: int) -> LifecycleDecision:
    if policy.delete_after_days is not None and age_days >= policy.delete_after_days:
        return LifecycleDecision(
            "DELETE", f"上架 {age_days} 天,达到删除阈值({policy.delete_after_days} 天)"
        )
    if policy.delist_after_days is not None and age_days >= policy.delist_after_days:
        return LifecycleDecision(
            "DELIST", f"上架 {age_days} 天,达到下架阈值({policy.delist_after_days} 天)"
        )
    return LifecycleDecision(KEEP, "未达到动作条件")


def _within(moment: datetime | None, now: datetime, days: int) -> bool:
    if moment is None or days <= 0:
        return False
    reference = _as_utc(moment)
    return (_as_utc(now) - reference) < timedelta(days=days)


def _age_days(listed_at: datetime | None, now: datetime) -> int | None:
    if listed_at is None:
        return None
    return max((_as_utc(now) - _as_utc(listed_at)).days, 0)


def _as_utc(moment: datetime) -> datetime:
    """SQLite 读出的 datetime 是 naive 的 UTC 值,这里统一补上 tzinfo。"""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
