"""库存池与库存单元的状态定义(Issue #5 Phase 6)。

这套状态机存在的理由只有一条:一条卡密只能被一个订单拿走。所以"预留"是显式占用,"消费"是
终态,而"结果未知"必须冻结——既不能当成功,也不能当没发生过,只能等人工确认或对账收尾。

状态值直接存字符串,三库一致;转换规则集中在这里,不允许各处自行 UPDATE 状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from xianyu_agent.domain.transitions import (
    InvalidStateTransition,
    can_transition,
    require_transition,
)

__all__ = [
    "CONSUMABLE_POOL_TYPES",
    "POOL_TYPES",
    "RESERVATION_STATES",
    "RESERVATION_TRANSITIONS",
    "UNIT_STATES",
    "UNIT_TRANSITIONS",
    "InvalidStateTransition",
    "InventoryPoolType",
    "InventoryUnitState",
    "ReservationState",
    "can_transition",
    "is_reservation_state",
    "is_unit_state",
    "require_transition",
]


class InventoryPoolType(StrEnum):
    """库存池的履约形态。"""

    TEXT = "text"  # 固定文案(可含白名单变量),不消耗库存
    DATA = "data"  # 逐行卡密,一条一个单元,必须原子消费
    IMAGE = "image"  # 图片地址,不消耗库存
    API = "api"  # 由外部 API 取件,副作用在远端


class InventoryUnitState(StrEnum):
    """单条库存单元(一条卡密)的状态。"""

    AVAILABLE = "available"
    RESERVED = "reserved"
    CONSUMED = "consumed"
    UNCERTAIN = "uncertain"


class ReservationState(StrEnum):
    """一次预留的状态,与它占用的单元同步推进。"""

    RESERVED = "reserved"
    CONSUMED = "consumed"
    RELEASED = "released"
    UNCERTAIN = "uncertain"


POOL_TYPES: tuple[str, ...] = tuple(t.value for t in InventoryPoolType)
UNIT_STATES: tuple[str, ...] = tuple(s.value for s in InventoryUnitState)
RESERVATION_STATES: tuple[str, ...] = tuple(s.value for s in ReservationState)

# 只有 DATA 池按"一条卡密一个单元"消耗;text/image/api 是策略型池,不持有可消耗单元。
CONSUMABLE_POOL_TYPES: tuple[str, ...] = (InventoryPoolType.DATA.value,)

UNIT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    InventoryUnitState.AVAILABLE.value: frozenset({InventoryUnitState.RESERVED.value}),
    InventoryUnitState.RESERVED.value: frozenset(
        {
            InventoryUnitState.CONSUMED.value,
            InventoryUnitState.AVAILABLE.value,
            InventoryUnitState.UNCERTAIN.value,
        }
    ),
    InventoryUnitState.UNCERTAIN.value: frozenset(
        {InventoryUnitState.CONSUMED.value, InventoryUnitState.AVAILABLE.value}
    ),
    InventoryUnitState.CONSUMED.value: frozenset(),
}

RESERVATION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    ReservationState.RESERVED.value: frozenset(
        {
            ReservationState.CONSUMED.value,
            ReservationState.RELEASED.value,
            ReservationState.UNCERTAIN.value,
        }
    ),
    ReservationState.UNCERTAIN.value: frozenset(
        {ReservationState.CONSUMED.value, ReservationState.RELEASED.value}
    ),
    ReservationState.CONSUMED.value: frozenset(),
    ReservationState.RELEASED.value: frozenset(),
}


def is_unit_state(value: str) -> bool:
    return value in UNIT_STATES


def is_reservation_state(value: str) -> bool:
    return value in RESERVATION_STATES
