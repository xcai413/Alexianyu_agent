"""Message persistence helpers (pure domain logic, async DB IO at the boundary)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from xianyu_agent.db import Account, Conversation, Message, get_async_session
from xianyu_agent.domain.events import (
    MessageContentType,
    MessageDirection,
    MessageReceived,
    MessageSent,
)

logger = logging.getLogger(__name__)

BotState = Literal["enabled", "paused"]
TakeoverState = Literal["bot", "human"]


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


async def record_outbound(
    event: MessageSent,
    *,
    raw_payload: dict[str, Any] | None = None,
) -> int | None:
    """Persist a sent message and its redacted transport evidence.

    ACK-created rows and later seller pushes converge on the platform message id.
    When the push carries protocol metadata, preserve it for source attribution.
    """
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
                metadata_changed = _merge_outbound_metadata(
                    existing,
                    event,
                    raw_payload=raw_payload,
                )
                conversation_changed = await _merge_existing_conversation(
                    session,
                    conversation_id=existing.conversation_id,
                    buyer_id=event.receiver_id,
                    item_id=event.item_id,
                    observed_at=existing.sent_at or existing.received_at,
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
            raw_payload=raw_payload,
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
            metadata_changed = _merge_outbound_metadata(
                existing,
                event,
                raw_payload=raw_payload,
            )
            conversation_changed = await _merge_existing_conversation(
                session,
                conversation_id=existing.conversation_id,
                buyer_id=event.receiver_id,
                item_id=event.item_id,
                observed_at=existing.sent_at or existing.received_at,
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


async def mark_conversation_read(*, account_id: str, chat_id: str) -> int | None:
    """把会话的本地未读计数清零,返回清零前的值(查不到会话返回 None)。

    这是本地镜像记账:平台上"已读"是另一回事(需要真实上报)。分开处理是为了不把本地计数
    和平台状态混为一谈——平台没确认已读时,本地也不该假装买家消息已经处理过。
    """
    async with get_async_session() as session:
        account = await _get_account(session, account_id)
        if account is None:
            return None
        conversation = await _find_conversation(session, account_pk=account.id, chat_id=chat_id)
        if conversation is None:
            return None
        previous = int(conversation.unread_count or 0)
        conversation.unread_count = 0
        await session.commit()
    return previous


async def list_unread_messages(
    *, account_id: str, chat_id: str, limit: int = 100
) -> list[Message]:
    """列出该会话里带平台消息 ID 的入站消息,供已读上报使用。

    调用方通常还要知道"是不是没取全":所以在 limit 之外多读一条,由调用方判断截断。
    这里返回最多 ``limit`` 条,多出来的那条只用于判断存在性。
    """
    async with get_async_session() as session:
        account = await _get_account(session, account_id)
        if account is None:
            return []
        rows = (
            await session.scalars(
                select(Message)
                .where(
                    Message.account_id == account.id,
                    Message.chat_id == chat_id,
                    Message.direction == "inbound",
                )
                .order_by(Message.received_at.desc())
                .limit(limit + 1)
            )
        ).all()
    with_ids = [row for row in rows if (row.message_id or "").strip()]
    return with_ids[:limit]


async def has_more_inbound_messages_with_ids(
    *, account_id: str, chat_id: str, limit: int = 100
) -> bool:
    """是否还有超出 limit 的入站消息(用于避免"只上报一部分却清空未读")。"""
    async with get_async_session() as session:
        account = await _get_account(session, account_id)
        if account is None:
            return False
        total = await session.scalar(
            select(func.count(Message.id)).where(
                Message.account_id == account.id,
                Message.chat_id == chat_id,
                Message.direction == "inbound",
            )
        )
    return int(total or 0) > limit


async def set_bot_state(*, account_id: str, chat_id: str, state: BotState) -> Conversation | None:
    """Set one conversation's automatic-reply state.

    State changes require an existing canonical conversation, so a typo cannot
    create a detached control record that silently affects a later buyer chat.
    """
    return await _set_conversation_state(
        account_id=account_id,
        chat_id=chat_id,
        column="bot_state",
        value=state,
    )


async def set_takeover_state(
    *, account_id: str, chat_id: str, state: TakeoverState
) -> Conversation | None:
    """Set one conversation's bot/human ownership state."""
    return await _set_conversation_state(
        account_id=account_id,
        chat_id=chat_id,
        column="takeover_state",
        value=state,
    )


async def is_auto_reply_enabled(*, account_id: str, chat_id: str) -> bool:
    """Whether the canonical conversation permits automated replies.

    Missing conversations remain enabled for backwards compatibility. Inbound
    persistence normally creates the conversation before ReplyEngine runs.
    """
    conversation = await get_conversation(account_id=account_id, chat_id=chat_id)
    return conversation is None or (
        conversation.bot_state == "enabled" and conversation.takeover_state == "bot"
    )


async def _set_conversation_state(
    *, account_id: str, chat_id: str, column: Literal["bot_state", "takeover_state"], value: str
) -> Conversation | None:
    async with get_async_session() as session:
        account = await _get_account(session, account_id)
        if account is None:
            return None
        conversation = await _find_conversation(session, account_pk=account.id, chat_id=chat_id)
        if conversation is None:
            return None
        setattr(conversation, column, value)
        await session.commit()
        await session.refresh(conversation)
        return conversation


async def get_bargain_round(*, account_id: str, chat_id: str) -> int:
    """Return how many bargaining rounds this conversation already used."""
    conversation = await get_conversation(account_id=account_id, chat_id=chat_id)
    return 0 if conversation is None else conversation.bargain_round


async def advance_bargain_round(*, account_id: str, chat_id: str) -> int | None:
    """Count one more bargaining round; None when the conversation is unknown.

    Rounds are only consumed when a reply actually concedes ground, so a reply
    that repeats the listed price does not shorten the operator's allowance.
    """
    async with get_async_session() as session:
        account = await _get_account(session, account_id)
        if account is None:
            return None
        conversation = await _find_conversation(session, account_pk=account.id, chat_id=chat_id)
        if conversation is None:
            return None
        conversation.bargain_round = conversation.bargain_round + 1
        await session.commit()
        await session.refresh(conversation)
        return conversation.bargain_round


async def _find_conversation(session, *, account_pk: int, chat_id: str) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.account_id == account_pk,
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


def _merge_outbound_metadata(
    existing: Message,
    event: MessageSent,
    *,
    raw_payload: dict[str, Any] | None = None,
) -> bool:
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
    if existing.raw_payload is None and raw_payload is not None:
        existing.raw_payload = raw_payload
        changed = True
    return changed


async def _get_account(session, account_id: str) -> Account | None:
    return (
        await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
    ).scalar_one_or_none()
