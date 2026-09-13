"""Runtime composition for canonical outbound message sending."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from xianyu_agent.application.message.send_service import SendAttemptStatus, SendMessageService
from xianyu_agent.db import Account, Order, get_async_session
from xianyu_agent.infrastructure.message import AuditLogMessageAttemptRecorder, DomainMessageStore
from xianyu_agent.protocol.ws.message_adapter import WsClientMessageProtocol


async def send_worker_text(
    client: Any,
    *,
    account_id: str,
    chat_id: str,
    text: str,
) -> bool:
    """Send one worker-originated message through the canonical application boundary.

    Existing virtual-delivery wiring passes an external order id in the historical
    ``chat_id`` slot. Until Phase 5 owns an order-to-conversation binding, preserve
    that already-shipped delivery path explicitly instead of fabricating a chat id.
    Ordinary message/reply traffic always uses ``SendMessageService``.
    """
    store = DomainMessageStore()
    receiver_id = await store.resolve_receiver(account_id=account_id, chat_id=chat_id)
    if receiver_id is None and await _is_legacy_delivery_order(account_id, chat_id):
        return bool(await client.send_text(text))

    service = SendMessageService(
        WsClientMessageProtocol(client),
        AuditLogMessageAttemptRecorder(),
        store,
    )
    result = await service.send_text(
        account_id=account_id,
        chat_id=chat_id,
        receiver_id=receiver_id,
        text=text,
    )
    return result.status is SendAttemptStatus.SUCCESS


async def _is_legacy_delivery_order(account_id: str, order_id: str) -> bool:
    async with get_async_session() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == account_id).limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            return False
        order = (
            await session.execute(
                select(Order)
                .where(Order.account_id == account.id, Order.order_id == order_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return order is not None
