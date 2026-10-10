"""库存单元领域模型(Issue #5 Phase 6)。

一条 DATA 卡密 = 一个单元。单元是不可分割的占用单位:要么归某个订单,要么空闲,不存在
"半个单元"。单元内容属于敏感数据,任何日志或审计只能打印掩码预览。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from .state import UNIT_STATES, UNIT_TRANSITIONS, InventoryUnitState, require_transition

MASK_KEEP = 2


def masked_preview(value: str, *, keep: int = MASK_KEEP) -> str:
    """Audit-safe preview: keep a short head and tail, hide the middle."""
    text = value.strip()
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}***{text[-keep:]}"


@dataclass(frozen=True)
class InventoryUnit:
    """One consumable card code inside a DATA pool."""

    pool_id: int
    content: str
    state: str = InventoryUnitState.AVAILABLE.value
    reserved_by: str | None = None
    reservation_id: int | None = None
    reserved_at: datetime | None = None
    consumed_at: datetime | None = None
    external_ref: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    local_id: int | None = None

    def __post_init__(self) -> None:
        if self.pool_id <= 0:
            msg = "库存单元必须属于一个已存在的库存池"
            raise ValueError(msg)
        if not self.content.strip():
            msg = "库存单元内容不能为空"
            raise ValueError(msg)
        if self.state not in UNIT_STATES:
            msg = f"未知库存单元状态: {self.state!r},可选 {UNIT_STATES}"
            raise ValueError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "库存单元 id 必须为正整数"
            raise ValueError(msg)
        if self.state == InventoryUnitState.AVAILABLE.value and self.reserved_by is not None:
            msg = "空闲单元不应带有占用方"
            raise ValueError(msg)
        if self.state == InventoryUnitState.RESERVED.value and not self.reserved_by:
            msg = "预留单元必须记录占用方"
            raise ValueError(msg)

    def masked(self) -> str:
        return masked_preview(self.content)

    def transition_to(
        self,
        target: str,
        *,
        job_ref: str | None = None,
        reservation_id: int | None = None,
        at: datetime | None = None,
    ) -> InventoryUnit:
        """Return a copy in the target state, rejecting illegal transitions."""
        if target not in UNIT_STATES:
            msg = f"未知库存单元状态: {target!r},可选 {UNIT_STATES}"
            raise ValueError(msg)
        subject = f"库存单元 #{self.local_id or '?'}"
        require_transition(UNIT_TRANSITIONS, self.state, target, subject=subject)
        if target == InventoryUnitState.RESERVED.value:
            if not job_ref:
                msg = "预留状态必须记录占用方"
                raise ValueError(msg)
            return replace(
                self,
                state=target,
                reserved_by=job_ref,
                reservation_id=reservation_id,
                reserved_at=at,
            )
        if target == InventoryUnitState.AVAILABLE.value:
            return replace(
                self,
                state=target,
                reserved_by=None,
                reservation_id=None,
                reserved_at=None,
            )
        if target == InventoryUnitState.CONSUMED.value:
            return replace(self, state=target, consumed_at=at)
        return replace(self, state=target)
