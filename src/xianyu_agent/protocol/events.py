"""Normalized protocol DTOs and transport/runtime events for xianyu-agent.

Raw WebSocket frames are parsed into these compatibility models before any later
protocol-to-domain adaptation. The six business-shaped ``EventEnvelope`` subclasses
remain here for legacy consumers; canonical Domain Events live in
``xianyu_agent.domain.events`` and are produced by ``protocol.event_mapper``.

Conventions:
  - Timestamps are stored as ``datetime`` in UTC.
  - String IDs may be optional because Xianyu sometimes pushes partial events.
  - ``raw`` is truncated protocol/debug material. It remains accessible for legacy
    persistence/redaction code but is hidden from normal repr output and must never
    cross into canonical Domain Events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
#  Enums
# ============================================================================


class MessageDirection(StrEnum):
    INBOUND = "inbound"  # buyer -> seller
    OUTBOUND = "outbound"  # seller -> buyer


class MessageContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    CARD = "card"
    PRODUCT = "product"
    SYSTEM = "system"


class ConnectionState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"
    ERROR = "error"


# ============================================================================
#  Raw frame (what the WS client receives from the network)
# ============================================================================


class WsFrame(BaseModel):
    """A raw WebSocket frame from the Xianyu push channel.

    ``body`` may be a JSON string, base64-encoded JSON, or normalized mapping.
    Downstream protocol parsers decide how to interpret it.
    """

    model_config = ConfigDict(extra="allow")

    code: int = 0
    packet_id: str | None = Field(default=None, alias="packetId")
    headers: dict[str, Any] = Field(default_factory=dict)
    body: str | dict[str, Any] | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ============================================================================
#  Legacy normalized transport DTOs
# ============================================================================


class EventEnvelope(BaseModel):
    """Compatibility envelope produced by the protocol parser.

    This is not the canonical Domain Event base. ``raw`` is transport-only debug
    material and is intentionally excluded from repr to reduce accidental disclosure.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    account_id: str
    received_at: datetime
    raw: dict[str, Any] | None = Field(default=None, repr=False)


class MessageReceived(EventEnvelope):
    """Legacy normalized DTO for an inbound chat message."""

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


class MessageSent(EventEnvelope):
    """Legacy normalized DTO for an outbound chat message."""

    chat_id: str
    message_id: str | None = None
    item_id: str | None = None
    sent_at: datetime | None = None
    receiver_id: str
    direction: Literal[MessageDirection.OUTBOUND] = MessageDirection.OUTBOUND
    content_type: MessageContentType = MessageContentType.TEXT
    content: str


class OrderCreated(EventEnvelope):
    """Legacy normalized DTO for an order-created observation."""

    order_id: str
    item_id: str | None = None
    item_title: str | None = None
    buyer_id: str
    buyer_name: str | None = None
    amount: float = 0.0


class OrderPaid(EventEnvelope):
    """Legacy normalized DTO for an order-paid observation."""

    order_id: str
    item_id: str | None = None
    item_title: str | None = None
    buyer_id: str
    buyer_name: str | None = None
    amount: float = 0.0
    paid_at: datetime | None = None


class OrderDelivered(EventEnvelope):
    """Legacy normalized DTO for an order-delivered observation."""

    order_id: str
    delivered_at: datetime | None = None


class SystemNotice(EventEnvelope):
    """Legacy normalized DTO for a non-chat system notice."""

    notice_type: str
    content: str


class ConnectionStateChanged(BaseModel):
    """Transport/runtime event emitted by the client rather than parsed upstream."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    account_id: str
    state: ConnectionState
    detail: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ErrorOccurred(BaseModel):
    """Transport/runtime event emitted when the client or parser fails."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    account_id: str
    code: str
    message: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
