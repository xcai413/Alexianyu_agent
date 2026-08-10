"""Message persistence helpers (pure domain logic, async DB IO at the boundary)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select

from xianyu_agent.db import Account, Message, get_async_session
from xianyu_agent.protocol.events import (
    MessageContentType,
    MessageDirection,
    MessageReceived,
    MessageSent,
)

logger = logging.getLogger(__name__)


async def upsert_inbound(event: MessageReceived) -> int | None:
    """Persist a received message. Returns the row id, or None if the
    owning account is unknown. Idempotent on `message_id` when present.
    """
    async with get_async_session() as session:
        account = await _get_account(session, event.account_id)
        if account is None:
            return None
        if event.message_id:
            existing = (
                await session.execute(
                    select(Message).where(Message.message_id == event.message_id).limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
        msg = Message(
            account_id=account.id,
            chat_id=event.chat_id,
            message_id=event.message_id,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            direction=MessageDirection.INBOUND.value,
            content_type=event.content_type.value
            if isinstance(event.content_type, MessageContentType)
            else str(event.content_type),
            content=event.content,
            image_url=event.image_url,
            raw_payload=event.raw,
            received_at=event.received_at,
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg.id


async def record_outbound(event: MessageSent) -> int | None:
    """Persist a sent message (for audit trail)."""
    async with get_async_session() as session:
        account = await _get_account(session, event.account_id)
        if account is None:
            return None
        msg = Message(
            account_id=account.id,
            chat_id=event.chat_id,
            message_id=event.message_id,
            sender_id=event.account_id,
            sender_name=None,
            direction=MessageDirection.OUTBOUND.value,
            content_type=event.content_type.value
            if isinstance(event.content_type, MessageContentType)
            else str(event.content_type),
            content=event.content,
            received_at=event.received_at,
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg.id


async def list_recent(
    *,
    account_id: str,
    since: datetime | None = None,
    limit: int = 50,
    direction: str | None = None,
    chat_id: str | None = None,
) -> Sequence[Message]:
    """Query recent messages for an account."""
    async with get_async_session() as session:
        account = await _get_account(session, account_id)
        if account is None:
            return []
        stmt = select(Message).where(Message.account_id == account.id)
        if since is not None:
            stmt = stmt.where(Message.received_at >= since)
        if direction:
            stmt = stmt.where(Message.direction == direction)
        if chat_id:
            stmt = stmt.where(Message.chat_id == chat_id)
        stmt = stmt.order_by(Message.received_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())


async def get_by_id(message_id: int) -> Message | None:
    async with get_async_session() as session:
        return (
            await session.execute(select(Message).where(Message.id == message_id).limit(1))
        ).scalar_one_or_none()


async def _get_account(session, account_id: str) -> Account | None:
    return (
        await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
    ).scalar_one_or_none()
