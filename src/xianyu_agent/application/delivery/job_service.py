"""发货任务的应用层编排(Issue #5 Phase 6, §10-§16)。

职责边界:这里只管任务本身的状态与尝试记录。真正的外部动作(校验订单、预留库存、发送消息、
调用发卡 API)由 6G 的执行器编排,失败时把"失败发生在发送前还是发送后"告诉这里,由
classify_failure 决定是重试、终态还是冻结。

重复的 OrderPaid 用 create_or_get 收敛:先插入,撞唯一键就读回既有记录——判断依据是数据库
约束,不是内存状态。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from xianyu_agent.application.inventory.reservation_service import InventoryReservationService
from xianyu_agent.db import Account, AuditLog, Order, get_async_session
from xianyu_agent.domain.delivery.job import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_SEMANTIC_KEY,
    AttemptResult,
    DeliveryAttempt,
    DeliveryJob,
    DeliveryJobState,
    FailureKind,
    classify_failure,
)
from xianyu_agent.domain.inventory.reservation import ReservationState
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyDeliveryJobRepository,
    SqlAlchemyInventoryRepository,
)

FAILURE_RESULTS: dict[FailureKind, str] = {
    FailureKind.NOT_SENT: AttemptResult.FAILED_NOT_SENT.value,
    FailureKind.REJECTED: AttemptResult.FAILED_REJECTED.value,
    FailureKind.UNKNOWN: AttemptResult.UNKNOWN.value,
    FailureKind.BLOCKED: AttemptResult.BLOCKED.value,
}


class DeliveryJobService:
    """Create, advance and audit delivery jobs."""

    async def create_or_get(
        self,
        *,
        account_id: str,
        order_id: str,
        required_quantity: int = 1,
        semantic_key: str = DEFAULT_SEMANTIC_KEY,
        item_id: str | None = None,
        sku_ref: str | None = None,
        policy_snapshot: dict[str, object] | None = None,
    ) -> DeliveryJob:
        """Return the existing job for this order, or create the first one."""
        job = DeliveryJob(
            account_id=account_id,
            order_id=order_id,
            required_quantity=required_quantity,
            semantic_key=semantic_key,
            item_id=item_id,
            sku_ref=sku_ref,
            policy_snapshot=dict(policy_snapshot or {}),
        )
        created: DeliveryJob | None = None
        try:
            async with get_async_session() as session, session.begin():
                repo = SqlAlchemyDeliveryJobRepository(session)
                job_id = await repo.add(job)
                created = await repo.get(job_id)
        except IntegrityError:
            # 唯一键命中:重复的 OrderPaid 或恢复重放,读回既有任务。
            created = None
        if created is not None:
            return created
        existing = await self.get_by_order(account_id, order_id, semantic_key=semantic_key)
        if existing is None:  # pragma: no cover - 唯一键冲突后必然读得到
            msg = f"发货任务创建冲突但读不到既有记录: {job.identity}"
            raise RuntimeError(msg)
        return existing

    async def get(self, job_id: int) -> DeliveryJob:
        async with get_async_session() as session:
            job = await SqlAlchemyDeliveryJobRepository(session).get(job_id)
        if job is None:
            msg = f"发货任务 #{job_id} 不存在"
            raise ValueError(msg)
        return job

    async def get_by_order(
        self,
        account_id: str,
        order_id: str,
        *,
        semantic_key: str = DEFAULT_SEMANTIC_KEY,
    ) -> DeliveryJob | None:
        async with get_async_session() as session:
            return await SqlAlchemyDeliveryJobRepository(session).get_by_identity(
                account_id, order_id, semantic_key
            )

    async def set_state(
        self,
        job_id: int,
        target: str,
        *,
        last_error: str | None = None,
        at: datetime | None = None,
    ) -> DeliveryJob:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryJobRepository(session)
            job = await repo.get(job_id)
            if job is None:
                msg = f"发货任务 #{job_id} 不存在"
                raise ValueError(msg)
            updated = job.transition_to(target, at=at or datetime.now(UTC), last_error=last_error)
            await repo.apply(updated)
            return updated

    async def attach_reservation(self, job_id: int, reservation_id: int) -> DeliveryJob:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryJobRepository(session)
            job = await repo.get(job_id)
            if job is None:
                msg = f"发货任务 #{job_id} 不存在"
                raise ValueError(msg)
            updated = DeliveryJob(
                account_id=job.account_id,
                order_id=job.order_id,
                required_quantity=job.required_quantity,
                semantic_key=job.semantic_key,
                state=job.state,
                policy_snapshot=job.policy_snapshot,
                reservation_id=reservation_id,
                item_id=job.item_id,
                sku_ref=job.sku_ref,
                last_error=job.last_error,
                local_id=job.local_id,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
            )
            await repo.apply(updated)
            return updated

    async def record_attempt(
        self,
        job_id: int,
        *,
        action: str,
        result: str,
        external_message_id: str | None = None,
        error: str | None = None,
    ) -> DeliveryAttempt:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryJobRepository(session)
            attempt_no = await repo.count_attempts(job_id) + 1
            attempt = DeliveryAttempt(
                job_id=job_id,
                attempt_no=attempt_no,
                action=action,
                result=result,
                external_message_id=external_message_id,
                error=error,
                finished_at=datetime.now(UTC),
            )
            await repo.add_attempt(attempt)
            return attempt

    async def attempts(self, job_id: int) -> list[DeliveryAttempt]:
        async with get_async_session() as session:
            return await SqlAlchemyDeliveryJobRepository(session).list_attempts(job_id)

    async def register_failure(
        self,
        job_id: int,
        kind: FailureKind,
        *,
        error: str,
        action: str = "deliver",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> DeliveryJob:
        """Record one failed attempt and move the job per the retry rules."""
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryJobRepository(session)
            job = await repo.get(job_id)
            if job is None:
                msg = f"发货任务 #{job_id} 不存在"
                raise ValueError(msg)
            attempt_no = await repo.count_attempts(job_id) + 1
            decision = classify_failure(kind, attempt_no=attempt_no, max_attempts=max_attempts)
            await repo.add_attempt(
                DeliveryAttempt(
                    job_id=job_id,
                    attempt_no=attempt_no,
                    action=action,
                    result=FAILURE_RESULTS[kind],
                    error=error,
                    finished_at=datetime.now(UTC),
                )
            )
            updated = job.transition_to(
                decision.state,
                at=datetime.now(UTC),
                last_error=error or decision.reason,
            )
            await repo.apply(updated)
            return updated

    async def retry(self, job_id: int, *, force: bool = False) -> DeliveryJob:
        """Move a retryable job back to checking; force only reopens a definite not-sent end state."""
        if not force:
            return await self.set_state(job_id, DeliveryJobState.CHECKING.value)
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryJobRepository(session)
            job = await repo.get(job_id)
            if job is None:
                msg = f"发货任务 #{job_id} 不存在"
                raise ValueError(msg)
            if job.state != DeliveryJobState.FAILED_FINAL.value:
                msg = f"只有 failed_final 任务可以强制重试,当前是 {job.state}"
                raise ValueError(msg)
            attempts = await repo.list_attempts(job_id)
            if not attempts or attempts[-1].result != AttemptResult.FAILED_NOT_SENT.value:
                msg = "只有最后一次尝试明确未发送的任务可以强制重试"
                raise ValueError(msg)
            reservation_to_replace = False
            if job.reservation_id is not None:
                reservation = await SqlAlchemyInventoryRepository(session).get_reservation(
                    job.reservation_id
                )
                if reservation is None:
                    msg = "任务库存预留记录不存在,不能强制重试"
                    raise ValueError(msg)
                if reservation.state == ReservationState.RELEASED.value:
                    pass
                elif attempts[-1].action == "reserve":
                    # A prior send was manually judged not delivered, but its
                    # consumed card remains frozen as evidence. A new retry
                    # must allocate fresh stock instead of reusing that card.
                    reservation_to_replace = True
                else:
                    msg = "任务仍有未释放库存预留,不能强制重试"
                    raise ValueError(msg)
            reopened = replace(
                job,
                state=DeliveryJobState.CHECKING.value,
                reservation_id=None if reservation_to_replace else job.reservation_id,
                completed_at=None,
                last_error="人工确认明确未发送后重试",
            )
            await repo.apply(reopened)
            return reopened

    async def cancel(self, job_id: int, *, reason: str | None = None) -> DeliveryJob:
        return await self.set_state(job_id, DeliveryJobState.CANCELLED.value, last_error=reason)

    async def resolve_uncertain(
        self,
        job_id: int,
        *,
        delivered: bool,
        note: str,
        resolved_by: str = "operator",
        actor: str = "cli",
    ) -> DeliveryJob:
        """人工或对账给出的结论:确认外部收到了就完成,确认没收到才允许重试。

        三条纪律,与自动化收尾保持一致:

        1. **只有 `uncertain` 能被人工收尾**。其它状态是自动流程写下的运行事实,人工改写会把
            历史抹平,所以直接拒绝而不是"顺手改一下"。
        2. **必须写明依据**。这条结论会替代掉失败原因,没有依据的记录等于把证据丢了。
        3. **任务与库存一起收尾**。任务冻结时库存也会被冻结(见执行器的 `_reconcile_inventory`),
            只收一半会留下永久冻结的卡密或"已发出却退回"的库存,所以这里两条一起落。
        """
        text = (note or "").strip()
        if not text:
            msg = "人工收尾必须写明依据(例如平台订单页已确认收到卡密)"
            raise ValueError(msg)
        by = (resolved_by or "").strip() or "operator"
        target = (
            DeliveryJobState.COMPLETED.value
            if delivered
            else DeliveryJobState.FAILED_RETRYABLE.value
        )
        async with get_async_session() as session:
            current = await SqlAlchemyDeliveryJobRepository(session).get(job_id)
        if current is None:
            msg = f"发货任务 #{job_id} 不存在"
            raise ValueError(msg)
        if current.state != DeliveryJobState.UNCERTAIN.value:
            msg = (
                f"发货任务 #{job_id} 当前状态是 {current.state},不是结果不明,无需人工收尾"
            )
            raise ValueError(msg)

        # 先落库存:它失败时任务状态保持不变,不会出现"任务已完成但卡密还冻着"。
        stock = await self._settle_reservation(current.reservation_id, delivered=delivered)
        job = await self.set_state(
            job_id, target, last_error=f"人工收尾({by}): {text}"[:255]
        )
        if not delivered:
            async with get_async_session() as session, session.begin():
                account = (
                    await session.execute(
                        select(Account).where(Account.account_id == job.account_id).limit(1)
                    )
                ).scalar_one_or_none()
                if account is not None:
                    order = (
                        await session.execute(
                            select(Order)
                            .where(
                                Order.account_id == account.id,
                                Order.order_id == job.order_id,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if order is not None:
                        order.delivered_at = None
        await self._write_resolve_audit(
            job=job,
            delivered=delivered,
            actor=actor,
            has_note=bool(text),
            stock=stock,
        )
        return job

    async def flag_uncertain(self, job_id: int, *, reason: str) -> DeliveryJob:
        """人工指出已完成任务未能确认送达;保留已消耗库存,禁止原卡复用。"""
        text = (reason or "").strip()
        if not text:
            raise ValueError("标记结果不明必须写明依据")
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryJobRepository(session)
            current = await repo.get(job_id)
            if current is None:
                raise ValueError(f"发货任务 #{job_id} 不存在")
            if current.state != DeliveryJobState.COMPLETED.value:
                raise ValueError(f"只有 completed 任务可以标记结果不明,当前是 {current.state}")
            updated = replace(
                current,
                state=DeliveryJobState.UNCERTAIN.value,
                completed_at=datetime.now(UTC),
                last_error=f"人工标记结果不明: {text}"[:255],
            )
            await repo.apply(updated)
            session.add(
                AuditLog(
                    actor="cli",
                    action="delivery.job.flagged_uncertain",
                    target=current.order_id,
                    params={"job_id": job_id, "has_reason": True},
                    result="success",
                )
            )
            return updated

    async def _settle_reservation(
        self, reservation_id: int | None, *, delivered: bool
    ) -> dict[str, object]:
        """把任务对应的库存预留一起收尾;已经收尾过的预留不再重复处理。"""
        if reservation_id is None:
            return {"reservation_id": None, "action": "none"}
        reservations = InventoryReservationService()
        reservation = await reservations.get(reservation_id)
        if reservation.state not in {
            ReservationState.RESERVED.value,
            ReservationState.UNCERTAIN.value,
        }:
            return {
                "reservation_id": reservation_id,
                "action": "already_settled",
                "state": reservation.state,
            }
        units = await reservations.resolve_uncertain(reservation_id, delivered=delivered)
        return {
            "reservation_id": reservation_id,
            "action": "consumed" if delivered else "released",
            "units": units,
        }

    async def _write_resolve_audit(
        self,
        *,
        job: DeliveryJob,
        delivered: bool,
        actor: str,
        has_note: bool,
        stock: dict[str, object],
    ) -> None:
        """记一条人工收尾审计:只记判定与库存去向,依据正文留在任务上,不进审计参数。"""
        async with get_async_session() as session:
            session.add(
                AuditLog(
                    actor=actor,
                    action="delivery.job.resolved",
                    target=job.order_id,
                    params={
                        "job_id": job.local_id,
                        "delivered": delivered,
                        "has_note": has_note,
                        "stock": stock,
                    },
                    result="success",
                )
            )
            await session.commit()

    async def list(
        self,
        *,
        account_id: str | None = None,
        order_id: str | None = None,
        state: str | None = None,
        states: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[DeliveryJob]:
        async with get_async_session() as session:
            return await SqlAlchemyDeliveryJobRepository(session).list_jobs(
                account_id=account_id,
                order_id=order_id,
                state=state,
                states=states,
                limit=limit,
            )
