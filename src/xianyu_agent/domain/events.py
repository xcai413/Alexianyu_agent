"""Canonical business-facing domain events.

These models are transport-neutral. Protocol/debug payloads, frame headers, cookies,
tokens, and other raw upstream material must not cross this boundary. The protocol
adapter is responsible for constructing these models field-by-field.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict


class MessageDirection(StrEnum):
    """Canonical chat direction from the seller account's perspective."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageContentType(StrEnum):
    """Canonical normalized message content categories."""

    TEXT = "text"
    IMAGE = "image"
    CARD = "card"
    PRODUCT = "product"
    SYSTEM = "system"


class DomainEvent(BaseModel):
    """Common metadata for canonical domain events.

    ``received_at`` is retained as the stable observation timestamp from the existing
    protocol contract. Raw transport payloads are intentionally not part of this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    account_id: str
    received_at: datetime


class MessageReceived(DomainEvent):
    """A buyer sent a message to the seller account."""

    chat_id: str
    message_id: str | None = None
    item_id: str | None = None
    sent_at: datetime | None = None
    sender_id: str
    sender_name: str | None = None
    direction: Literal[MessageDirection.INBOUND] = MessageDirection.INBOUND
    content_type: MessageContentType = MessageContentType.TEXT
    content: str
    image_url: str | None = None


class MessageSent(DomainEvent):
    """The seller account sent a message acknowledged by the platform."""

    chat_id: str
    message_id: str | None = None
    item_id: str | None = None
    sent_at: datetime | None = None
    receiver_id: str
    direction: Literal[MessageDirection.OUTBOUND] = MessageDirection.OUTBOUND
    content_type: MessageContentType = MessageContentType.TEXT
    content: str


class OrderCreated(DomainEvent):
    """A buyer created an order."""

    order_id: str
    item_id: str | None = None
    item_title: str | None = None
    buyer_id: str
    buyer_name: str | None = None
    amount: float = 0.0


class OrderPaid(DomainEvent):
    """A buyer paid for an order."""

    order_id: str
    item_id: str | None = None
    item_title: str | None = None
    buyer_id: str
    buyer_name: str | None = None
    amount: float = 0.0
    paid_at: datetime | None = None


class OrderDelivered(DomainEvent):
    """An order was marked as delivered."""

    order_id: str
    delivered_at: datetime | None = None


class SystemNotice(DomainEvent):
    """A normalized non-chat system notice."""

    notice_type: str
    content: str


DomainEventVariant: TypeAlias = (
    MessageReceived
    | MessageSent
    | OrderCreated
    | OrderPaid
    | OrderDelivered
    | SystemNotice
)
