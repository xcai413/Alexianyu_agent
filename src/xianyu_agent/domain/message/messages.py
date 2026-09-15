"""Message persistence helpers (pure domain logic, async DB IO at the boundary)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from xianyu_agent.db import Account, Conversation, Message, get_async_session
from xianyu_agent.domain.events import (
    MessageContentType,
    MessageDirection,
    MessageReceived,
    MessageSent,
)

logger = logging.getLogger(__name__)


async def upsert_inbound(
    event: MessageReceived,
    *,
    raw_payload: dict[str, Any] | None = None,
) -> int | None:
    """Persist a received message and optional separately-redacted transport payload.

    Returns the row id, or None if the owning account is unknown. Idempotent on
    the account-scoped external message id when present. The legacy ``message_id``
    column mirrors that external id during the staged migration. ``raw_payload`` is
    persistence metadata and is not part of the canonical Domain Event contract.
    """
    async with get_async_session() as session:
        account = await _get_account(session, event.account_id)
        if account is None:
            return None
        if event.message_id:
            existing = await _find_message_by_platform_id(
                session,
                account_id=account.id,
                message_id=event.message_id,
            )
            if existing is not None:
                return existing.id
        conversation = await _get_or_create_conversation(
            session,
            account_id=account.id,
            chat_id=event.chat_id,
            buyer_id=event.sender_id,
            item_id=event.item_id,
            observed_at=event.sent_at or event.received_at,
        )
        msg = Message(
            account_id=account.id,
            conversation_id=conversation.id,
            chat_id=event.chat_id,
            external_message_id=event.message_id,
            message_id=event.message_id,
            item_id=event.item_id,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            direction=MessageDirection.INBOUND.value,
            content_type=event.content_type.value
            if isinstance(event.content_type, MessageContentType)
            else str(event.content_type),
            content=event.content,
            image_url=event.image_url,
            raw_payload=raw_payload,
            received_at=event.received_at,
            sent_at=event.sent_at,
        )
        session.add(msg)
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(unread_count=Conversation.unread_count + 1)
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if not event.message_id:
                raise
            existing = await _find_message_by_platform_id(
                session,
                account_id=account.id,
                message_id=event.message_id,
            )
            if existing is None:
                raise
            return existing.id
        await session.refresh(msg)
        return msg.id


async def record_outbound(event: MessageSent) -> int | None:
    """Persist a sent message, atomically converging on a platform message id."""
    async with get_async_session() as session:
        account = await _get_account(session, event.account_id)
        if account is None:
            return None
        account_pk = account.id
        if event.message_id:
            existing = await _find_message_by_platform_id(
                session,
                account_id=account_pk,
                message_id=event.message_id,
            )
            if existing is not None:
                metadata_changed = _merge_outbound_metadata(existing, event)
                conversation_changed = await _merge_existing_conversation(
                    session,
                    conversation_id=existing.conversation_id,
                    buyer_id=event.receiver_id,
                    item_id=event.item_id,
                    observed_at=event.sent_at or event.received_at,
                )
                if metadata_changed or conversation_changed:
                    await session.commit()
                return existing.id

        conversation = await _get_or_create_conversation(
            session,
            account_id=account_pk,
            chat_id=event.chat_id,
            buyer_id=event.receiver_id,
            item_id=event.item_id,
            observed_at=event.sent_at or event.received_at,
        )
        msg = Message(
            account_id=account_pk,
            conversation_id=conversation.id,
            chat_id=event.chat_id,
            external_message_id=event.message_id,
            message_id=event.message_id,
            item_id=event.item_id,
            sender_id=event.account_id,
            sender_name=None,
            direction=MessageDirection.OUTBOUND.value,
            content_type=event.content_type.value
            if isinstance(event.content_type, MessageContentType)
            else str(event.content_type),
            content=event.content,
            received_at=event.received_at,
            sent_at=event.sent_at,
        )
        session.add(msg)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if not event.message_id:
                raise
            existing = await _find_message_by_platform_id(
                session,
                account_id=account_pk,
                message_id=event.message_id,
            )
            if existing is None:
                raise
            metadata_changed = _merge_outbound_metadata(existing, event)
            conversation_changed = await _merge_existing_conversation(
                session,
                conversation_id=existing.conversation_id,
                buyer_id=event.receiver_id,
                item_id=event.item_id,
                observed_at=event.sent_at or event.received_at,
            )
            if metadata_changed or conversation_changed:
                await session.commit()
            return existing.id
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


async def get_conversation(*, account_id: str, chat_id: str) -> Conversation | None:
    """Return the canonical conversation for one account-scoped chat id."""
    async with get_async_session() as session:
        account = await _get_account(session, account_id)
        if account is None:
            return None
        return (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.account_id == account.id,
                    Conversation.external_conversation_id == chat_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()


async def _find_message_by_platform_id(
    session, *, account_id: int, message_id: str
) -> Message | None:
    return (
        await session.execute(
            select(Message)
            .where(
                Message.account_id == account_id,
                or_(
                    Message.external_message_id == message_id,
                    Message.message_id == message_id,
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _get_or_create_conversation(
    session,
    *,
    account_id: int,
    chat_id: str,
    buyer_id: str | None,
    item_id: str | None,
    observed_at: datetime,
) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.account_id == account_id,
                Conversation.external_conversation_id == chat_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if conversation is None:
        try:
            async with session.begin_nested():
                conversation = Conversation(
                    account_id=account_id,
                    external_conversation_id=chat_id,
                    buyer_id=_normalized_buyer_id(buyer_id),
                    item_id=item_id,
                    last_message_at=observed_at,
                )
                session.add(conversation)
                await session.flush()
        except IntegrityError:
            conversation = (
                await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.account_id == account_id,
                        Conversation.external_conversation_id == chat_id,
                    )
                    .limit(1)
                )
            ).scalar_one()
    assert conversation is not None
    _merge_conversation_metadata(
        conversation,
        buyer_id=buyer_id,
        item_id=item_id,
        observed_at=observed_at,
    )
    return conversation


def _is_after(candidate: datetime, current: datetime | None) -> bool:
    """Compare SQLite's naive timestamps safely with canonical UTC events."""
    if current is None:
        return True
    normalized_candidate = candidate.replace(tzinfo=UTC) if candidate.tzinfo is None else candidate
    normalized_current = current.replace(tzinfo=UTC) if current.tzinfo is None else current
    return normalized_candidate > normalized_current


async def _merge_existing_conversation(
    session,
    *,
    conversation_id: int | None,
    buyer_id: str | None,
    item_id: str | None,
    observed_at: datetime,
) -> bool:
    if conversation_id is None:
        return False
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        return False
    return _merge_conversation_metadata(
        conversation,
        buyer_id=buyer_id,
        item_id=item_id,
        observed_at=observed_at,
    )


def _merge_conversation_metadata(
    conversation: Conversation,
    *,
    buyer_id: str | None,
    item_id: str | None,
    observed_at: datetime,
) -> bool:
    changed = False
    normalized_buyer_id = _normalized_buyer_id(buyer_id)
    if _normalized_buyer_id(conversation.buyer_id) is None and normalized_buyer_id is not None:
        conversation.buyer_id = normalized_buyer_id
        changed = True
    if conversation.item_id is None and item_id:
        conversation.item_id = item_id
        changed = True
    if _is_after(observed_at, conversation.last_message_at):
        conversation.last_message_at = observed_at
        changed = True
    return changed


def _normalized_buyer_id(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or candidate.casefold() == "unknown":
        return None
    return candidate


def _merge_outbound_metadata(existing: Message, event: MessageSent) -> bool:
    """Enrich an ACK-created row with later push metadata without duplicating it."""
    changed = False
    if existing.item_id is None and event.item_id is not None:
        existing.item_id = event.item_id
        changed = True
    if existing.sent_at is None and event.sent_at is not None:
        existing.sent_at = event.sent_at
        changed = True
    if existing.content is None and event.content:
        existing.content = event.content
        changed = True
    if existing.external_message_id is None and event.message_id:
        existing.external_message_id = event.message_id
        changed = True
    return changed


async def _get_account(session, account_id: str) -> Account | None:
    return (
        await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
    ).scalar_one_or_none()
