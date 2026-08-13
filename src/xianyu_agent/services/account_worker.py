"""AccountWorker: wraps a WsClient for one account and persists events.

One instance per account; owned by AccountPool. The worker owns the
WsClient lifecycle (start/stop) and routes parsed events into the domain
layer (messages/orders persistence). Heartbeats are written by the client.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, AuditLog, get_async_session
from xianyu_agent.domain import messages as domain_messages, orders as domain_orders
from xianyu_agent.protocol.client import WsClient
from xianyu_agent.protocol.events import (
    ConnectionState,
    EventEnvelope,
    MessageReceived,
    MessageSent,
    OrderCreated,
    OrderDelivered,
    OrderPaid,
    SystemNotice,
)
from xianyu_agent.services.delivery_service import DeliveryService
from xianyu_agent.services.guardrails import Guardrails, write_guardrail_event
from xianyu_agent.services.reply_engine import ReplyEngine


class AccountWorker:
    def __init__(
        self,
        account_id: str,
        *,
        client: WsClient | None = None,
        persist_events: bool = True,
        reply_engine: ReplyEngine | None = None,
        delivery_service: DeliveryService | None = None,
        guardrails: Guardrails | None = None,
        automation_mode: str | None = None,
    ) -> None:
        self.account_id = account_id
        self.started_at: datetime | None = None
        self._reply_engine = reply_engine
        self._delivery_service = delivery_service
        self._guardrails = guardrails
        self._automation_mode = automation_mode or get_settings().automation_mode
        self._client = client or WsClient(
            account_id,
            on_event=self._on_event if persist_events else None,
        )
        if self._guardrails is None and self._automation_mode == "active":
            self._guardrails = Guardrails()
        if self._reply_engine is None and persist_events and self._automation_mode == "active":
            self._reply_engine = ReplyEngine(sender=self._send_reply)
        if (
            self._delivery_service is None
            and persist_events
            and self._automation_mode == "active"
        ):
            self._delivery_service = DeliveryService(
                sender=self._send_reply, guardrails=self._guardrails
            )

    async def _on_event(self, event: EventEnvelope) -> None:
        """Default persistence handler: write messages and orders to SQLite."""
        if isinstance(event, MessageReceived):
            message_id = await domain_messages.upsert_inbound(event)
            if self._reply_engine is not None and self._guardrails is not None:
                decision = await self._guardrails.check_message(event.account_id, event.content)
                if decision.allowed:
                    await self._reply_engine.handle(event, message_id=message_id)
                else:
                    await write_guardrail_event(
                        event.account_id,
                        rule="message_gate",
                        detail=decision.reason or "",
                    )
        elif isinstance(event, MessageSent):
            await domain_messages.record_outbound(event)
        elif isinstance(event, (OrderCreated, OrderPaid, OrderDelivered)):
            await domain_orders.upsert_from_event(event)
            if isinstance(event, OrderPaid) and self._delivery_service is not None:
                await self._delivery_service.deliver(event)
        elif isinstance(event, SystemNotice):
            await self._record_system_notice(event)

    async def _record_system_notice(self, event: SystemNotice) -> None:
        """系统提示单独入审计日志,永不进入买家消息自动化。"""
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == event.account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return
            session.add(
                AuditLog(
                    actor="system",
                    action="protocol.system_notice",
                    target=event.account_id,
                    params={
                        "notice_type": event.notice_type,
                        "content": event.content[:500],
                    },
                    result="observed",
                )
            )
            await session.commit()

    async def _send_reply(self, _account_id: str, _chat_id: str, text: str) -> bool:
        """Best-effort send over the WS client.

        Note: until the real outbound protocol is implemented (Phase 1 live
        wiring), this sends the raw text frame; the mock server / future
        real implementation routes it to the chat.
        """
        return await self._client.send_text(text)

    def start(self) -> None:
        """Begin the WS loop. Idempotent; safe to call from sync contexts."""
        self._client.start()
        self.started_at = datetime.now(UTC)

    def inject_frame(self, frame) -> None:
        """Feed a synthetic frame through the full pipeline (tests / offline demo)."""
        self._client.inject_frame(frame)

    async def send_text(self, text: str) -> bool:
        """Send a raw text frame via the live WS connection.

        Returns False when the worker has no live socket (offline); the caller
        decides how to surface the failure.
        """
        return await self._client.send_text(text)

    async def stop(self) -> None:
        await self._client.stop()
        self.started_at = None

    @property
    def state(self) -> ConnectionState:
        return self._client.state

    @property
    def is_running(self) -> bool:
        return self.started_at is not None
