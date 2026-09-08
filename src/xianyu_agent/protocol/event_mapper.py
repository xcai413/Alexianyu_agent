"""Adapter from normalized protocol DTOs to canonical domain events.

The mapper is intentionally explicit: it never forwards ``raw`` or unknown protocol
fields through a generic dump/copy operation. That makes the protocol/domain data
boundary auditable and keeps transport secrets and frame metadata out of Domain.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeAlias

from xianyu_agent.domain import events as domain_events
from xianyu_agent.protocol import events as protocol_events

ProtocolDomainEvent: TypeAlias = (
    protocol_events.MessageReceived
    | protocol_events.MessageSent
    | protocol_events.OrderCreated
    | protocol_events.OrderPaid
    | protocol_events.OrderDelivered
    | protocol_events.SystemNotice
)


def to_domain_event(event: ProtocolDomainEvent) -> domain_events.DomainEventVariant:
    """Map one legacy normalized protocol DTO to its canonical Domain Event."""

    if isinstance(event, protocol_events.MessageReceived):
        return domain_events.MessageReceived(
            event_id=event.event_id,
            account_id=event.account_id,
            received_at=event.received_at,
            chat_id=event.chat_id,
            message_id=event.message_id,
            item_id=event.item_id,
            sent_at=event.sent_at,
            sender_id=event.sender_id,
            sender_name=event.sender_name,
            direction=domain_events.MessageDirection(event.direction.value),
            content_type=domain_events.MessageContentType(event.content_type.value),
            content=event.content,
            image_url=event.image_url,
        )
    if isinstance(event, protocol_events.MessageSent):
        return domain_events.MessageSent(
            event_id=event.event_id,
            account_id=event.account_id,
            received_at=event.received_at,
            chat_id=event.chat_id,
            message_id=event.message_id,
            item_id=event.item_id,
            sent_at=event.sent_at,
            receiver_id=event.receiver_id,
            direction=domain_events.MessageDirection(event.direction.value),
            content_type=domain_events.MessageContentType(event.content_type.value),
            content=event.content,
        )
    if isinstance(event, protocol_events.OrderCreated):
        return domain_events.OrderCreated(
            event_id=event.event_id,
            account_id=event.account_id,
            received_at=event.received_at,
            order_id=event.order_id,
            item_id=event.item_id,
            item_title=event.item_title,
            buyer_id=event.buyer_id,
            buyer_name=event.buyer_name,
            amount=event.amount,
        )
    if isinstance(event, protocol_events.OrderPaid):
        return domain_events.OrderPaid(
            event_id=event.event_id,
            account_id=event.account_id,
            received_at=event.received_at,
            order_id=event.order_id,
            item_id=event.item_id,
            item_title=event.item_title,
            buyer_id=event.buyer_id,
            buyer_name=event.buyer_name,
            amount=event.amount,
            paid_at=event.paid_at,
        )
    if isinstance(event, protocol_events.OrderDelivered):
        return domain_events.OrderDelivered(
            event_id=event.event_id,
            account_id=event.account_id,
            received_at=event.received_at,
            order_id=event.order_id,
            delivered_at=event.delivered_at,
        )
    if isinstance(event, protocol_events.SystemNotice):
        return domain_events.SystemNotice(
            event_id=event.event_id,
            account_id=event.account_id,
            received_at=event.received_at,
            notice_type=event.notice_type,
            content=event.content,
        )
    raise TypeError(f"unsupported protocol event: {type(event).__name__}")


def to_domain_events(
    events: Iterable[ProtocolDomainEvent],
) -> list[domain_events.DomainEventVariant]:
    """Map a batch without retaining protocol DTO instances or raw payloads."""

    return [to_domain_event(event) for event in events]
