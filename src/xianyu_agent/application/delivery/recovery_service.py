"""发货任务的启动恢复(Issue #5 Phase 6, §22/M19)。

进程重启后不能"把所有非终态任务重新发一遍"。判定按外部副作用是否存在:

- pending / checking / reserving:还没产生外部副作用,可以重新进入调度;
- reserved:库存已经占住,保留预留,不因为重启自动释放;
- delivering / confirming:行动中被打断,发没发出去说不清 —— 按结果未知冻结,等人工或对账;
- failed_retryable:按重试策略处理,不在这里自动重试;
- uncertain / blocked / needs_confirmation:保持冻结,等人工;
- completed / failed_final / cancelled:终态,不动。

scan() 只读并解释;freeze_in_flight() 才把"发没发出去说不清"的任务真正冻结(连同库存一起冻结)。
"""

from __future__ import annotations

import contextlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from xianyu_agent.application.delivery.job_service import DeliveryJobService
from xianyu_agent.application.inventory import InventoryReservationService
from xianyu_agent.domain.delivery import DeliveryJob, DeliveryJobState

ACTION_RESCHEDULE = "reschedule"
ACTION_KEEP_RESERVATION = "keep_reservation"
ACTION_RETRY_PER_POLICY = "retry_per_policy"
ACTION_FREEZE = "freeze"
ACTION_WAIT_HUMAN = "wait_human"

NON_TERMINAL_STATES: tuple[str, ...] = (
    DeliveryJobState.PENDING.value,
    DeliveryJobState.CHECKING.value,
    DeliveryJobState.NEEDS_CONFIRMATION.value,
    DeliveryJobState.RESERVING.value,
    DeliveryJobState.RESERVED.value,
    DeliveryJobState.DELIVERING.value,
    DeliveryJobState.CONFIRMING.value,
    DeliveryJobState.BLOCKED.value,
    DeliveryJobState.FAILED_RETRYABLE.value,
    DeliveryJobState.UNCERTAIN.value,
)

IN_FLIGHT_STATES: tuple[str, ...] = (
    DeliveryJobState.DELIVERING.value,
    DeliveryJobState.CONFIRMING.value,
)

ACTIONS: dict[str, tuple[str, str]] = {
    DeliveryJobState.PENDING.value: (ACTION_RESCHEDULE, "还没有产生外部副作用,可重新进入调度"),
    DeliveryJobState.CHECKING.value: (ACTION_RESCHEDULE, "还在校验阶段,可重新进入调度"),
    DeliveryJobState.RESERVING.value: (ACTION_RESCHEDULE, "库存预留未完成,可重新进入调度"),
    DeliveryJobState.RESERVED.value: (ACTION_KEEP_RESERVATION, "库存已占住,保留预留不自动释放"),
    DeliveryJobState.DELIVERING.value: (ACTION_FREEZE, "发送中被打断,结果未知,冻结待人工"),
    DeliveryJobState.CONFIRMING.value: (ACTION_FREEZE, "确认阶段被打断,结果未知,冻结待人工"),
    DeliveryJobState.FAILED_RETRYABLE.value: (ACTION_RETRY_PER_POLICY, "按重试策略处理"),
    DeliveryJobState.UNCERTAIN.value: (ACTION_WAIT_HUMAN, "结果未知,等人工或对账"),
    DeliveryJobState.BLOCKED.value: (ACTION_WAIT_HUMAN, "被守卫阻断,等人工确认"),
    DeliveryJobState.NEEDS_CONFIRMATION.value: (ACTION_WAIT_HUMAN, "需要人工确认后才能继续"),
}


@dataclass(frozen=True)
class RecoveryItem:
    job: DeliveryJob
    action: str
    reason: str


@dataclass(frozen=True)
class RecoveryReport:
    items: tuple[RecoveryItem, ...] = ()
    frozen: tuple[int, ...] = ()

    def by_action(self) -> dict[str, int]:
        return dict(Counter(item.action for item in self.items))

    @property
    def needs_human(self) -> tuple[RecoveryItem, ...]:
        return tuple(
            item for item in self.items if item.action in {ACTION_WAIT_HUMAN, ACTION_FREEZE}
        )


class DeliveryRecoveryService:
    """Inspect non-terminal jobs and freeze the ones whose outcome is unknown."""

    def __init__(
        self,
        *,
        jobs: DeliveryJobService | None = None,
        reservations: InventoryReservationService | None = None,
    ) -> None:
        self._jobs = jobs or DeliveryJobService()
        self._reservations = reservations or InventoryReservationService()

    async def scan(self, *, account_id: str | None = None, limit: int = 200) -> RecoveryReport:
        jobs = await self._jobs.list(account_id=account_id, states=NON_TERMINAL_STATES, limit=limit)
        items: list[RecoveryItem] = []
        for job in jobs:
            action, reason = ACTIONS.get(job.state, (ACTION_WAIT_HUMAN, "状态需要人工检查"))
            items.append(RecoveryItem(job=job, action=action, reason=reason))
        return RecoveryReport(items=tuple(items))

    async def freeze_in_flight(
        self, *, account_id: str | None = None, limit: int = 200
    ) -> RecoveryReport:
        """把"发没发出去说不清"的任务连同库存一起冻结。"""
        jobs = await self._jobs.list(account_id=account_id, states=IN_FLIGHT_STATES, limit=limit)
        frozen: list[int] = []
        items: list[RecoveryItem] = []
        for job in jobs:
            if job.local_id is None:
                continue
            if job.reservation_id is not None:
                # 库存可能已经被收尾过,冻结任务本身更重要。
                with contextlib.suppress(Exception):
                    await self._reservations.mark_uncertain(job.reservation_id)
            updated = await self._jobs.set_state(
                job.local_id,
                DeliveryJobState.UNCERTAIN.value,
                last_error=f"重启恢复:{job.state} 阶段被打断,结果未知",
                at=datetime.now(UTC),
            )
            frozen.append(job.local_id)
            items.append(
                RecoveryItem(job=updated, action=ACTION_FREEZE, reason="已冻结,等待人工或对账")
            )
        report = await self.scan(account_id=account_id, limit=limit)
        return RecoveryReport(
            items=tuple(items)
            + tuple(item for item in report.items if item.job.local_id not in set(frozen)),
            frozen=tuple(frozen),
        )
