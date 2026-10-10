"""预留领域模型(Issue #5 Phase 6, M15/M16)。

预留是订单与库存之间的契约:一次 reserve(N) 要么拿到 N 条单元,要么一条都不拿(ALL_OR_NOTHING)。
数量不足时不允许部分预留——那会让订单发出一半卡密,比直接失败更糟。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from .state import (
    RESERVATION_STATES,
    RESERVATION_TRANSITIONS,
    ReservationState,
    require_transition,
)


class InsufficientStock(RuntimeError):
    """可消耗单元不足,整次预留被拒绝。"""

    def __init__(self, *, requested: int, available: int, pool_id: int | None = None) -> None:
        self.requested = requested
        self.available = available
        self.pool_id = pool_id
        where = f"库存池 #{pool_id} " if pool_id is not None else "库存池 "
        super().__init__(f"{where}可用单元 {available} 条,不足以预留 {requested} 条")


class ConcurrentReservationError(RuntimeError):
    """并发预留撞上同一条单元:本次事务整体回滚。"""


@dataclass(frozen=True)
class Reservation:
    """One all-or-nothing claim of inventory units by a delivery job."""

    pool_id: int
    job_ref: str
    quantity: int
    unit_ids: tuple[int, ...] = ()
    state: str = ReservationState.RESERVED.value
    local_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.pool_id <= 0:
            msg = "预留必须属于一个已存在的库存池"
            raise ValueError(msg)
        if not self.job_ref.strip():
            msg = "预留必须记录占用方(job_ref)"
            raise ValueError(msg)
        if self.quantity < 1:
            msg = "预留数量至少为 1"
            raise ValueError(msg)
        if self.state not in RESERVATION_STATES:
            msg = f"未知预留状态: {self.state!r},可选 {RESERVATION_STATES}"
            raise ValueError(msg)
        if self.unit_ids and len(self.unit_ids) != self.quantity:
            msg = "预留单元数与预留数量不一致"
            raise ValueError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "预留 id 必须为正整数"
            raise ValueError(msg)

    @property
    def is_terminal(self) -> bool:
        return not RESERVATION_TRANSITIONS.get(self.state, frozenset())

    def transition_to(self, target: str) -> Reservation:
        """Return a copy in the target state, rejecting illegal transitions."""
        if target not in RESERVATION_STATES:
            msg = f"未知预留状态: {target!r},可选 {RESERVATION_STATES}"
            raise ValueError(msg)
        subject = f"预留 #{self.local_id or '?'}"
        require_transition(RESERVATION_TRANSITIONS, self.state, target, subject=subject)
        return replace(self, state=target)


def plan_reservation(available_unit_ids: Sequence[int], quantity: int) -> tuple[int, ...]:
    """Pick unit ids in stable order, or refuse the whole reservation.

    The caller passes whatever the database currently reports as available; this
    function only enforces ALL_OR_NOTHING and stable ordering.
    """
    if quantity < 1:
        msg = "预留数量至少为 1"
        raise ValueError(msg)
    ordered = tuple(available_unit_ids)
    if len(ordered) < quantity:
        raise InsufficientStock(requested=quantity, available=len(ordered))
    return ordered[:quantity]
