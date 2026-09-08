"""DeliveryService: OrderPaid -> pick card -> consume -> send -> mark delivered."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select, update

from xianyu_agent.db import Account, Card, CardConsumption, Order, get_async_session
from xianyu_agent.db.models import OrderStatus
from xianyu_agent.domain import orders as domain_orders
from xianyu_agent.domain.inventory import cards as domain_cards
from xianyu_agent.protocol.events import OrderPaid
from xianyu_agent.services.guardrails import Guardrails, write_guardrail_event

logger = logging.getLogger(__name__)

DeliverySender = Callable[[str, str, str], Awaitable[bool]]  # (account_id, order_id, content)


class DeliveryResult:
    def __init__(
        self,
        *,
        delivered: bool,
        order_id: str,
        card_id: int | None = None,
        code: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.delivered = delivered
        self.order_id = order_id
        self.card_id = card_id
        self.code = code
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"DeliveryResult(delivered={self.delivered}, order={self.order_id}, "
            f"card={self.card_id}, reason={self.reason})"
        )


class DeliveryService:
    def __init__(
        self,
        sender: DeliverySender | None = None,
        *,
        guardrails: Guardrails | None = None,
    ) -> None:
        self._sender = sender
        self._guardrails = guardrails

    async def deliver(self, event: OrderPaid) -> DeliveryResult:
        """Full delivery pipeline for a paid order."""
        order_id = await domain_orders.upsert_from_event(event)
        if order_id is None:
            return DeliveryResult(delivered=False, order_id=event.order_id, reason="order 未入库")
        if self._guardrails is not None:
            decision = await self._guardrails.check_delivery(event.account_id, event.amount)
            if not decision.allowed:
                await write_guardrail_event(
                    event.account_id,
                    rule="order_amount",
                    detail=decision.reason or "",
                )
                await self._mark_blocked(order_id, f"guardrail: {decision.reason}")
                logger.warning("delivery blocked by guardrail: %s", decision.reason)
                return DeliveryResult(
                    delivered=False,
                    order_id=event.order_id,
                    reason=f"guardrail: {decision.reason}",
                )

        # 1. pick first enabled card with stock for the account
        card = await self._pick_card(event.account_id)
        if card is None:
            await self._mark_no_stock(order_id, event.order_id)
            return DeliveryResult(
                delivered=False,
                order_id=event.order_id,
                reason="无可用卡密(未配置或无库存)",
            )

        # 2. consume one code
        code = await domain_cards.consume_card(card.id, order_id)
        if code is None:
            await self._mark_no_stock(order_id, event.order_id)
            return DeliveryResult(
                delivered=False,
                order_id=event.order_id,
                card_id=card.id,
                reason="卡密扣减失败(可能已被并发消耗)",
            )

        # 3. send to buyer
        ok = False
        fail_reason: str | None = None
        if self._sender is not None:
            try:
                ok = await self._sender(event.account_id, event.order_id, code)
            except Exception as exc:
                fail_reason = f"{type(exc).__name__}: {exc}"
                logger.warning("delivery send failed for %s: %s", event.order_id, fail_reason)
            if not ok and fail_reason is None:
                fail_reason = "发送器返回失败"
        else:
            fail_reason = "未配置发送器(sender=None)"

        # 4. record outcome on order + consumption
        await self._record_outcome(order_id, delivered=ok, code=code, fail_reason=fail_reason)
        if ok:
            logger.info("delivered order=%s card=%s", event.order_id, card.id)
            return DeliveryResult(
                delivered=True, order_id=event.order_id, card_id=card.id, code=code
            )
        return DeliveryResult(
            delivered=False,
            order_id=event.order_id,
            card_id=card.id,
            code=code,
            reason=fail_reason or "发送失败",
        )

    async def retry(self, order_id: int) -> DeliveryResult:
        """Retry delivery of a previously failed order.

        Reuses the code reserved in the failed consumption row so the same
        card is never handed to two orders. Orders blocked before any
        consumption (guardrail / no stock) are reported back with a hint.
        """
        async with get_async_session() as session:
            row = (
                await session.execute(select(Order).where(Order.id == order_id).limit(1))
            ).scalar_one_or_none()
            if row is None:
                return DeliveryResult(delivered=False, order_id=str(order_id), reason="订单不存在")
            if row.status == OrderStatus.DELIVERED.value:
                return DeliveryResult(
                    delivered=True, order_id=row.order_id, reason="订单已发货,无需重试"
                )
            account_row = (
                await session.execute(
                    select(Account).where(Account.id == row.account_id).limit(1)
                )
            ).scalar_one_or_none()
            cons = (
                await session.execute(
                    select(CardConsumption)
                    .where(CardConsumption.order_id == order_id)
                    .where(CardConsumption.status == "failed")
                    .order_by(CardConsumption.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if cons is None or not cons.content:
            return DeliveryResult(
                delivered=False,
                order_id=row.order_id,
                reason="无预留卡密(未扣减或 guardrail 拦截),先检查订单失败原因",
            )
        account_key = account_row.account_id if account_row is not None else str(row.account_id)
        # Guardrails still apply on retry (e.g. amount ceiling).
        if self._guardrails is not None:
            decision = await self._guardrails.check_delivery(account_key, float(row.amount or 0))
            if not decision.allowed:
                await write_guardrail_event(
                    account_key,
                    rule="order_amount",
                    detail=decision.reason or "",
                )
                return DeliveryResult(
                    delivered=False,
                    order_id=row.order_id,
                    reason=f"guardrail: {decision.reason}",
                )
        code = cons.content
        ok = False
        fail_reason: str | None = None
        if self._sender is not None:
            try:
                ok = await self._sender(account_key, row.order_id, code)
            except Exception as exc:
                fail_reason = f"{type(exc).__name__}: {exc}"
                logger.warning("delivery retry send failed for %s: %s", row.order_id, fail_reason)
            if not ok and fail_reason is None:
                fail_reason = "发送器返回失败"
        else:
            fail_reason = "未配置发送器(sender=None)"
        await self._record_outcome(row.id, delivered=ok, code=code, fail_reason=fail_reason)
        if ok:
            logger.info("retry delivered order=%s", row.order_id)
            return DeliveryResult(delivered=True, order_id=row.order_id, code=code)
        return DeliveryResult(
            delivered=False,
            order_id=row.order_id,
            code=code,
            reason=fail_reason or "发送失败",
        )

    async def _pick_card(self, account_id: str) -> Card | None:
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return None
            stmt = (
                select(Card)
                .where(Card.account_id == account.id)
                .where(Card.enabled.is_(True))
                .where(Card.remaining > 0)
                .order_by(Card.id.asc())
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _mark_no_stock(self, order_id: int, _display_order_id: str) -> None:
        async with get_async_session() as session:
            row = (
                await session.execute(select(Order).where(Order.id == order_id).limit(1))
            ).scalar_one_or_none()
            if row is None:
                return
            row.delivery_fail_reason = "无可用卡密"
            await session.commit()

    async def _mark_blocked(self, order_id: int, reason: str) -> None:
        async with get_async_session() as session:
            row = (
                await session.execute(select(Order).where(Order.id == order_id).limit(1))
            ).scalar_one_or_none()
            if row is None:
                return
            row.delivery_fail_reason = reason
            await session.commit()

    async def _record_outcome(
        self, order_id: int, *, delivered: bool, code: str, fail_reason: str | None
    ) -> None:
        async with get_async_session() as session:
            row = (
                await session.execute(select(Order).where(Order.id == order_id).limit(1))
            ).scalar_one_or_none()
            if row is None:
                return
            if delivered:
                row.status = OrderStatus.DELIVERED.value
                row.delivered_at = datetime.now(UTC)
                row.delivery_content = code
                row.delivery_fail_reason = None
            else:
                row.delivery_fail_reason = fail_reason
            # mark the consumption row accordingly
            await session.execute(
                update(CardConsumption)
                .where(CardConsumption.order_id == order_id)
                .values(
                    status="success" if delivered else "failed",
                    error=None if delivered else fail_reason,
                )
            )
            await session.commit()
