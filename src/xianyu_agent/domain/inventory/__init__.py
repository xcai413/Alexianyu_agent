"""库存领域:库存池、可消费单元、预留,以及旧卡密模型的兼容层。"""

from .pool import MAX_DELAY_SECONDS, InventoryPool, UnitImportSummary, plan_unit_import
from .reservation import (
    ConcurrentReservationError,
    InsufficientStock,
    Reservation,
    plan_reservation,
)
from .state import (
    CONSUMABLE_POOL_TYPES,
    POOL_TYPES,
    RESERVATION_STATES,
    RESERVATION_TRANSITIONS,
    UNIT_STATES,
    UNIT_TRANSITIONS,
    InvalidStateTransition,
    InventoryPoolType,
    InventoryUnitState,
    ReservationState,
    can_transition,
    is_reservation_state,
    is_unit_state,
    require_transition,
)
from .unit import InventoryUnit, masked_preview

__all__ = [
    "CONSUMABLE_POOL_TYPES",
    "MAX_DELAY_SECONDS",
    "POOL_TYPES",
    "RESERVATION_STATES",
    "RESERVATION_TRANSITIONS",
    "UNIT_STATES",
    "UNIT_TRANSITIONS",
    "ConcurrentReservationError",
    "InsufficientStock",
    "InvalidStateTransition",
    "InventoryPool",
    "InventoryPoolType",
    "InventoryUnit",
    "InventoryUnitState",
    "Reservation",
    "ReservationState",
    "UnitImportSummary",
    "can_transition",
    "is_reservation_state",
    "is_unit_state",
    "masked_preview",
    "plan_reservation",
    "plan_unit_import",
    "require_transition",
]
