"""规格 → 库存来源 → 预留:多规格发货的应用编排(Issue #5 Phase 6)。

买家选中的规格组合必须在本地镜像里精确命中一条绑定:命中 pool 就按绑定数量原子预留;
命中不了、绑定停用、或者绑的是旧卡密组,都明确失败——不回退到别的规格,也不"随便发一个"。
Phase 6 的自动发货只走 pool 绑定,旧 card 绑定保留给既有卡密链路。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from xianyu_agent.application.inventory import InventoryReservationService
from xianyu_agent.domain.inventory import Reservation
from xianyu_agent.domain.item.sku import SpecValue
from xianyu_agent.domain.item.sku_binding import (
    CARD_TARGET,
    POOL_TARGET,
    resolve_item_binding,
)


@dataclass(frozen=True)
class SpecSourcePlan:
    """Which inventory source serves one buyer selection, and why."""

    item_id: str
    selection_key: str
    matched: bool
    target_kind: str | None = None
    target_id: int | None = None
    quantity: int = 0
    reason: str = ""

    def describe(self) -> str:
        if not self.matched:
            return f"未命中({self.reason})"
        return f"{self.target_kind}:{self.target_id} x{self.quantity}"


class SpecSourceService:
    """Resolve a spec selection and reserve the inventory it points at."""

    def __init__(self, *, reservations: InventoryReservationService | None = None) -> None:
        self._reservations = reservations or InventoryReservationService()

    async def plan(
        self,
        account_id: str,
        item_id: str,
        selection: Mapping[str, str] | Sequence[SpecValue],
    ) -> SpecSourcePlan:
        resolution = await resolve_item_binding(account_id, item_id, selection)
        if resolution is None:
            return SpecSourcePlan(
                item_id=item_id,
                selection_key="",
                matched=False,
                reason=f"商品 {item_id} 不在本地镜像里,先运行 item sync",
            )
        binding = resolution.binding
        return SpecSourcePlan(
            item_id=item_id,
            selection_key=resolution.selection_key,
            matched=resolution.matched,
            target_kind=None if binding is None else binding.target_kind,
            target_id=None if binding is None else binding.target_id,
            quantity=0 if binding is None else binding.quantity,
            reason=resolution.reason,
        )

    async def reserve(
        self,
        account_id: str,
        item_id: str,
        selection: Mapping[str, str] | Sequence[SpecValue],
        *,
        job_ref: str,
    ) -> Reservation:
        """Reserve the bound inventory for one buyer selection, all or nothing."""
        plan = await self.plan(account_id, item_id, selection)
        if not plan.matched:
            raise ValueError(plan.reason)
        if plan.target_kind == CARD_TARGET:
            msg = (
                f"规格组合 {plan.selection_key} 绑的是旧卡密组,"
                "Phase 6 的自动发货只支持库存池绑定,请改用 --target pool:<池id>"
            )
            raise ValueError(msg)
        if plan.target_kind != POOL_TARGET or plan.target_id is None:
            msg = f"未知库存来源类型: {plan.target_kind}"
            raise ValueError(msg)
        return await self._reservations.reserve(plan.target_id, job_ref, plan.quantity)
