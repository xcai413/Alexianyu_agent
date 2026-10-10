"""Runtime composition for canonical outbound message sending."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from xianyu_agent.application.message.send_service import (
    SendAttemptStatus,
    SendMessageResult,
    SendMessageService,
)
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


async def send_worker_delivery_text(
    client: Any,
    *,
    account_id: str,
    order_id: str,
    text: str,
) -> SendMessageResult:
    """通过订单买家对应的真实会话发送卡密,并等待相关平台回执。"""
    async with get_async_session() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == account_id).limit(1)
            )
        ).scalar_one_or_none()
        order = None
        if account is not None:
            order = (
                await session.execute(
                    select(Order)
                    .where(Order.account_id == account.id, Order.order_id == order_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
    buyer_id = str(order.buyer_id or "").strip() if order is not None else ""
    if not buyer_id or buyer_id.casefold() == "unknown":
        return SendMessageResult(
            account_id=account_id,
            chat_id="",
            receiver_id=buyer_id,
            status=SendAttemptStatus.FAILED_RETRYABLE,
            retry_allowed=True,
            detail="订单没有可用买家标识,未发送",
        )
    store = DomainMessageStore()
    conversation_id = await store.resolve_conversation_id(
        account_id=account_id, buyer_id=buyer_id
    )
    if not conversation_id:
        return SendMessageResult(
            account_id=account_id,
            chat_id="",
            receiver_id=buyer_id,
            status=SendAttemptStatus.FAILED_RETRYABLE,
            retry_allowed=True,
            detail="订单买家没有可定位的聊天会话,未发送",
        )
    service = SendMessageService(
        WsClientMessageProtocol(client),
        AuditLogMessageAttemptRecorder(),
        store,
    )
    return await service.send_text(
        account_id=account_id,
        chat_id=conversation_id,
        receiver_id=buyer_id,
        text=text,
    )


async def send_worker_image(
    client: Any,
    *,
    account_id: str,
    chat_id: str,
    image_url: str,
    width: int = 800,
    height: int = 600,
) -> bool:
    """Send one image through the canonical boundary.

    图片没有"原始帧"兼容路径:必须能解析出会话接收方,否则宁可失败也不猜一个 chat id。
    """
    store = DomainMessageStore()
    receiver_id = await store.resolve_receiver(account_id=account_id, chat_id=chat_id)
    if receiver_id is None:
        return False
    service = SendMessageService(
        WsClientMessageProtocol(client),
        AuditLogMessageAttemptRecorder(),
        store,
    )
    result = await service.send_image(
        account_id=account_id,
        chat_id=chat_id,
        receiver_id=receiver_id,
        image_url=image_url,
        width=width,
        height=height,
    )
    return result.status is SendAttemptStatus.SUCCESS


async def _is_legacy_delivery_order(account_id: str, order_id: str) -> bool:
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
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
