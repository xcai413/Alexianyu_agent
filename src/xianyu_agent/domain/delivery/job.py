"""发货任务与服务尝试(Issue #5 Phase 6, §10/§11/§14/§16)。

一个订单只能有一个有效发货任务:唯一键是(账号, 订单, 语义键),重复的 OrderPaid、轮询与
恢复重放都必须收敛到同一条记录,不能靠内存锁。

状态机把"外面到底发生了没有"讲清楚:PENDING 还没动手,RESERVED 已经占了库存,
COMPLETED 是确认发出去,UNCERTAIN 是请求已发出但结果不明——后者既不自动重发也不自动退库存。

重试规则(M20):只有能证明没产生外部副作用的失败才允许自动重试;拿不准的失败一律按
UNCERTAIN 处理。这个判断必须由知道"失败发生在发送前还是发送后"的调用方显式给出类型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from xianyu_agent.domain.transitions import (
    InvalidStateTransition,
    can_transition,
    require_transition,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_SEMANTIC_KEY = "order_paid"


class DeliveryJobState(StrEnum):
    PENDING = "pending"
    CHECKING = "checking"
    NEEDS_CONFIRMATION = "needs_confirmation"
    RESERVING = "reserving"
    RESERVED = "reserved"
    DELIVERING = "delivering"
    CONFIRMING = "confirming"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


JOB_STATES: tuple[str, ...] = tuple(s.value for s in DeliveryJobState)
TERMINAL_JOB_STATES: frozenset[str] = frozenset(
    {
        DeliveryJobState.COMPLETED.value,
        DeliveryJobState.FAILED_FINAL.value,
        DeliveryJobState.CANCELLED.value,
    }
)

JOB_TRANSITIONS: Mapping[str, frozenset[str]] = {
    DeliveryJobState.PENDING.value: frozenset(
        {DeliveryJobState.CHECKING.value, DeliveryJobState.CANCELLED.value}
    ),
    DeliveryJobState.CHECKING.value: frozenset(
        {
            DeliveryJobState.RESERVING.value,
            DeliveryJobState.BLOCKED.value,
            DeliveryJobState.NEEDS_CONFIRMATION.value,
            DeliveryJobState.FAILED_RETRYABLE.value,
            DeliveryJobState.FAILED_FINAL.value,
            DeliveryJobState.CANCELLED.value,
        }
    ),
    DeliveryJobState.NEEDS_CONFIRMATION.value: frozenset(
        {DeliveryJobState.CHECKING.value, DeliveryJobState.CANCELLED.value}
    ),
    DeliveryJobState.RESERVING.value: frozenset(
        {
            DeliveryJobState.RESERVED.value,
            # api 池没有本地库存可占:取到卡密后直接进入发送阶段。
            DeliveryJobState.DELIVERING.value,
            DeliveryJobState.FAILED_RETRYABLE.value,
            DeliveryJobState.FAILED_FINAL.value,
            DeliveryJobState.UNCERTAIN.value,
        }
    ),
    DeliveryJobState.RESERVED.value: frozenset(
        {
            DeliveryJobState.DELIVERING.value,
            DeliveryJobState.FAILED_RETRYABLE.value,
            DeliveryJobState.FAILED_FINAL.value,
            DeliveryJobState.UNCERTAIN.value,
            DeliveryJobState.CANCELLED.value,
        }
    ),
    DeliveryJobState.DELIVERING.value: frozenset(
        {
            DeliveryJobState.CONFIRMING.value,
            DeliveryJobState.FAILED_RETRYABLE.value,
            DeliveryJobState.FAILED_FINAL.value,
            DeliveryJobState.UNCERTAIN.value,
        }
    ),
    DeliveryJobState.CONFIRMING.value: frozenset(
        {
            DeliveryJobState.COMPLETED.value,
            DeliveryJobState.FAILED_RETRYABLE.value,
            # 平台明确拒绝确认发货(例如订单状态错误)是终局结果,不是"结果不明";
            # 内容已经发给买家,重试发货只会重复打扰,必须留给人工收尾。
            DeliveryJobState.FAILED_FINAL.value,
            DeliveryJobState.UNCERTAIN.value,
        }
    ),
    DeliveryJobState.BLOCKED.value: frozenset(
        {
            # 重新校验:blocked 说的是"这次没放行",不是"永远不许再试"。运营把原因修掉
            # (买家付款、订单补进镜像、商品重新上架)之后,可以显式再跑一次。
            DeliveryJobState.CHECKING.value,
            DeliveryJobState.NEEDS_CONFIRMATION.value,
            DeliveryJobState.CANCELLED.value,
        }
    ),
    DeliveryJobState.FAILED_RETRYABLE.value: frozenset(
        {
            DeliveryJobState.CHECKING.value,
            DeliveryJobState.FAILED_FINAL.value,
            DeliveryJobState.CANCELLED.value,
        }
    ),
    # UNCERTAIN 只能由人工或对账收尾:确认已发就完成,确认没发才允许重试。
    DeliveryJobState.UNCERTAIN.value: frozenset(
        {
            DeliveryJobState.COMPLETED.value,
            DeliveryJobState.FAILED_RETRYABLE.value,
            DeliveryJobState.CANCELLED.value,
        }
    ),
    DeliveryJobState.COMPLETED.value: frozenset(),
    DeliveryJobState.FAILED_FINAL.value: frozenset(),
    DeliveryJobState.CANCELLED.value: frozenset(),
}


class FailureKind(StrEnum):
    """失败发生在哪一段,决定能不能自动重试。"""

    NOT_SENT = "not_sent"  # 请求没发出去(建连前失败 / 本地校验失败 / 明确未提交)
    REJECTED = "rejected"  # 对方明确拒绝,重试也不会成功
    UNKNOWN = "unknown"  # 请求已发出但结果不明
    BLOCKED = "blocked"  # 风控或守卫阻断


class AttemptResult(StrEnum):
    SUCCESS = "success"
    FAILED_NOT_SENT = "failed_not_sent"
    FAILED_REJECTED = "failed_rejected"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


# 发货任务的非法流转与库存共用同一个异常类型,上层可以一次捕获完。
InvalidJobTransition = InvalidStateTransition


@dataclass(frozen=True)
class RetryDecision:
    """What a failure means for the job, and whether automatic retry is allowed."""

    state: str
    retryable: bool
    reason: str

    @property
    def requires_human(self) -> bool:
        return self.state in {
            DeliveryJobState.UNCERTAIN.value,
            DeliveryJobState.NEEDS_CONFIRMATION.value,
            DeliveryJobState.BLOCKED.value,
        }


def classify_failure(
    kind: FailureKind,
    *,
    attempt_no: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RetryDecision:
    """Map one failure to a job state; conservative when the kind is unknown."""
    if attempt_no < 1:
        msg = "尝试次数从 1 开始计数"
        raise ValueError(msg)
    if kind is FailureKind.NOT_SENT:
        if attempt_no < max_attempts:
            return RetryDecision(
                state=DeliveryJobState.FAILED_RETRYABLE.value,
                retryable=True,
                reason=f"第 {attempt_no} 次在发送前失败,可以再试(上限 {max_attempts} 次)",
            )
        return RetryDecision(
            state=DeliveryJobState.FAILED_FINAL.value,
            retryable=False,
            reason=f"发送前已失败 {attempt_no} 次,达到重试上限",
        )
    if kind is FailureKind.REJECTED:
        return RetryDecision(
            state=DeliveryJobState.FAILED_FINAL.value,
            retryable=False,
            reason="对方明确拒绝,重试不会改变结果",
        )
    if kind is FailureKind.BLOCKED:
        return RetryDecision(
            state=DeliveryJobState.BLOCKED.value,
            retryable=False,
            reason="被发货守卫或风控阻断,需要人工确认",
        )
    return RetryDecision(
        state=DeliveryJobState.UNCERTAIN.value,
        retryable=False,
        reason="请求已发出但结果不明,冻结等待人工或对账",
    )


@dataclass(frozen=True)
class DeliveryJob:
    """One all-or-nothing delivery of an order."""

    account_id: str
    order_id: str
    required_quantity: int = 1
    semantic_key: str = DEFAULT_SEMANTIC_KEY
    state: str = DeliveryJobState.PENDING.value
    policy_snapshot: dict[str, object] = field(default_factory=dict)
    reservation_id: int | None = None
    item_id: str | None = None
    sku_ref: str | None = None
    last_error: str | None = None
    local_id: int | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            msg = "发货任务必须属于某个账号"
            raise ValueError(msg)
        if not self.order_id.strip():
            msg = "发货任务必须指向订单"
            raise ValueError(msg)
        if not self.semantic_key.strip():
            msg = "发货任务必须带语义键"
            raise ValueError(msg)
        if self.required_quantity < 1:
            msg = "发货数量至少为 1"
            raise ValueError(msg)
        if self.state not in JOB_STATES:
            msg = f"未知发货任务状态: {self.state!r},可选 {JOB_STATES}"
            raise ValueError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "发货任务 id 必须为正整数"
            raise ValueError(msg)

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.account_id, self.order_id, self.semantic_key)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES

    def can_transition_to(self, target: str) -> bool:
        return can_transition(JOB_TRANSITIONS, self.state, target)

    def transition_to(
        self,
        target: str,
        *,
        at: datetime | None = None,
        last_error: str | None = None,
    ) -> DeliveryJob:
        if target not in JOB_STATES:
            msg = f"未知发货任务状态: {target!r},可选 {JOB_STATES}"
            raise ValueError(msg)
        subject = f"发货任务 #{self.local_id or '?'}(订单 {self.order_id})"
        require_transition(JOB_TRANSITIONS, self.state, target, subject=subject)
        started = self.started_at
        if started is None and target != DeliveryJobState.PENDING.value:
            started = at
        completed = self.completed_at
        if target in TERMINAL_JOB_STATES or target == DeliveryJobState.UNCERTAIN.value:
            completed = at
        return replace(
            self,
            state=target,
            started_at=started,
            completed_at=completed,
            last_error=last_error if last_error is not None else self.last_error,
        )


@dataclass(frozen=True)
class DeliveryAttempt:
    """One external action tried for a job; attempt history is append-only."""

    job_id: int
    attempt_no: int
    action: str
    result: str
    external_message_id: str | None = None
    error: str | None = None
    local_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.job_id <= 0:
            msg = "尝试必须属于一个已存在的发货任务"
            raise ValueError(msg)
        if self.attempt_no < 1:
            msg = "尝试序号从 1 开始"
            raise ValueError(msg)
        if not self.action.strip():
            msg = "尝试必须记录动作"
            raise ValueError(msg)
        if not self.result.strip():
            msg = "尝试必须记录结果"
            raise ValueError(msg)
