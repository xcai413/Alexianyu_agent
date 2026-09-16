"""Canonical message lookup/persistence adapter for outbound sends."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from xianyu_agent.domain.events import MessageContentType, MessageSent
from xianyu_agent.domain.message import messages as domain_messages

_UNKNOWN_SENDER_ID = "unknown"


class DomainMessageStore:
    """Resolve peer identity and persist confirmed outbound messages."""

    async def resolve_receiver(self, *, account_id: str, chat_id: str) -> str | None:
        conversation = await domain_messages.get_conversation(
            account_id=account_id,
            chat_id=chat_id,
        )
        if conversation is not None and _is_known_party(conversation.buyer_id):
            return conversation.buyer_id.strip()
        rows = await domain_messages.list_recent(
            account_id=account_id,
            chat_id=chat_id,
            direction="inbound",
            limit=1,
        )
        if not rows:
            return None
        sender_id = rows[0].sender_id
        return sender_id.strip() if _is_known_party(sender_id) else None

    async def record_outbound(
        self,
        *,
        account_id: str,
        chat_id: str,
        receiver_id: str,
        text: str,
        client_message_id: str | None,
        response: Any,
    ) -> None:
        platform_message_id = _platform_message_id(response)
        if platform_message_id is None:
            # A correlated 200 response proves the write succeeded, but without the
            # platform message id there is no stable key to merge the later seller
            # push. Defer history persistence to that push instead of creating a
            # message_id=NULL row that would be duplicated when the push arrives.
            return
        row_id = await domain_messages.record_outbound(
            MessageSent(
                event_id=client_message_id or uuid.uuid4().hex,
                account_id=account_id,
                received_at=datetime.now(UTC),
                chat_id=chat_id,
                message_id=platform_message_id,
                receiver_id=receiver_id,
                content_type=MessageContentType.TEXT,
                content=text,
            )
        )
        if row_id is None:
            raise LookupError(f"unknown account for outbound message: {account_id}")


def _platform_message_id(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    body = response.get("body")
    if not isinstance(body, dict):
        return None
    value = body.get("messageId")
    if value is None:
        return None
    message_id = str(value).strip()
    return message_id or None


def _is_known_party(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().casefold() != _UNKNOWN_SENDER_ID
    )
