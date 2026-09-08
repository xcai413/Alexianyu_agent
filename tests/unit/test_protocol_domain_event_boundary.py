from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from pydantic import ValidationError

from xianyu_agent.domain import events as domain_events
from xianyu_agent.protocol import events as protocol_events
from xianyu_agent.protocol.event_mapper import (
    ProtocolDomainEvent,
    to_domain_event,
    to_domain_events,
)
from xianyu_agent.protocol.parser import parse_frame

NOW = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
RAW_SECRET = {
    "headers": {
        "cookie": "COOKIE-SECRET",
        "authorization": "Bearer TOKEN-SECRET",
    },
    "body": {"token": "TOKEN-SECRET"},
}


def _base() -> dict[str, object]:
    return {
        "event_id": "evt-1",
        "account_id": "seller-1",
        "received_at": NOW,
        "raw": RAW_SECRET,
    }


def _legacy_events() -> list[tuple[ProtocolDomainEvent, type[domain_events.DomainEvent]]]:
    base = _base()
    return [
        (
            protocol_events.MessageReceived(
                **base,
                chat_id="chat-1",
                message_id="m-1",
                item_id="item-1",
                sent_at=NOW,
                sender_id="buyer-1",
                sender_name="Buyer",
                content_type=protocol_events.MessageContentType.IMAGE,
                content="hello",
                image_url="https://example.invalid/image",
            ),
            domain_events.MessageReceived,
        ),
        (
            protocol_events.MessageSent(
                **base,
                chat_id="chat-2",
                message_id="m-2",
                item_id="item-2",
                sent_at=NOW,
                receiver_id="buyer-2",
                content_type=protocol_events.MessageContentType.CARD,
                content="sent",
            ),
            domain_events.MessageSent,
        ),
        (
            protocol_events.OrderCreated(
                **base,
                order_id="order-1",
                item_id="item-1",
                item_title="Item",
                buyer_id="buyer-1",
                buyer_name="Buyer",
                amount=12.5,
            ),
            domain_events.OrderCreated,
        ),
        (
            protocol_events.OrderPaid(
                **base,
                order_id="order-2",
                item_id="item-2",
                item_title="Item 2",
                buyer_id="buyer-2",
                buyer_name="Buyer 2",
                amount=20.0,
                paid_at=NOW,
            ),
            domain_events.OrderPaid,
        ),
        (
            protocol_events.OrderDelivered(
                **base,
                order_id="order-3",
                delivered_at=NOW,
            ),
            domain_events.OrderDelivered,
        ),
        (
            protocol_events.SystemNotice(
                **base,
                notice_type="system_tip",
                content="notice",
            ),
            domain_events.SystemNotice,
        ),
    ]


def test_domain_event_contract_has_no_raw_transport_field() -> None:
    event = domain_events.MessageReceived(
        event_id="evt-1",
        account_id="seller-1",
        received_at=NOW,
        chat_id="chat-1",
        sender_id="buyer-1",
        content="hello",
    )

    assert "raw" not in type(event).model_fields
    assert "raw" not in event.model_dump()

    with pytest.raises(ValidationError):
        domain_events.MessageReceived.model_validate(
            {
                **event.model_dump(),
                "raw": RAW_SECRET,
            }
        )


def test_domain_event_contract_is_immutable() -> None:
    event = domain_events.SystemNotice(
        event_id="evt-1",
        account_id="seller-1",
        received_at=NOW,
        notice_type="general",
        content="notice",
    )

    with pytest.raises(ValidationError):
        event.content = "mutated"


@pytest.mark.parametrize(("legacy", "expected_type"), _legacy_events())
def test_protocol_business_dtos_map_to_canonical_domain_events(
    legacy: ProtocolDomainEvent,
    expected_type: type[domain_events.DomainEvent],
) -> None:
    mapped = to_domain_event(legacy)

    assert type(mapped) is expected_type
    assert mapped.event_id == legacy.event_id
    assert mapped.account_id == legacy.account_id
    assert mapped.received_at == legacy.received_at
    assert "raw" not in mapped.model_dump()


def test_message_mapper_preserves_normalized_business_fields_and_enum_semantics() -> None:
    legacy = protocol_events.MessageReceived(
        **_base(),
        chat_id="chat-1",
        message_id="m-1",
        item_id="item-1",
        sent_at=NOW,
        sender_id="buyer-1",
        sender_name="Buyer",
        content_type=protocol_events.MessageContentType.PRODUCT,
        content="product",
        image_url="https://example.invalid/image",
    )

    mapped = to_domain_event(legacy)

    assert isinstance(mapped, domain_events.MessageReceived)
    assert mapped.direction is domain_events.MessageDirection.INBOUND
    assert mapped.content_type is domain_events.MessageContentType.PRODUCT
    assert mapped.chat_id == "chat-1"
    assert mapped.message_id == "m-1"
    assert mapped.item_id == "item-1"
    assert mapped.sent_at == NOW
    assert mapped.sender_id == "buyer-1"
    assert mapped.sender_name == "Buyer"
    assert mapped.content == "product"
    assert mapped.image_url == "https://example.invalid/image"
    assert domain_events.MessageDirection is not protocol_events.MessageDirection


def test_order_mapper_preserves_business_fields() -> None:
    legacy = protocol_events.OrderPaid(
        **_base(),
        order_id="order-1",
        item_id="item-1",
        item_title="Item",
        buyer_id="buyer-1",
        buyer_name="Buyer",
        amount=19.9,
        paid_at=NOW,
    )

    mapped = to_domain_event(legacy)

    assert isinstance(mapped, domain_events.OrderPaid)
    assert mapped.order_id == "order-1"
    assert mapped.item_id == "item-1"
    assert mapped.item_title == "Item"
    assert mapped.buyer_id == "buyer-1"
    assert mapped.buyer_name == "Buyer"
    assert mapped.amount == 19.9
    assert mapped.paid_at == NOW


def test_raw_secret_material_stays_on_legacy_protocol_side() -> None:
    legacy = protocol_events.SystemNotice(
        **_base(),
        notice_type="general",
        content="notice",
    )

    assert legacy.raw == RAW_SECRET
    assert "TOKEN-SECRET" not in repr(legacy)
    assert "COOKIE-SECRET" not in repr(legacy)
    assert legacy.model_dump()["raw"] == RAW_SECRET

    mapped = to_domain_event(legacy)
    serialized = mapped.model_dump_json()

    assert "raw" not in mapped.model_dump()
    assert "TOKEN-SECRET" not in serialized
    assert "COOKIE-SECRET" not in serialized


def test_legacy_protocol_event_copy_contract_remains_available() -> None:
    legacy = protocol_events.SystemNotice(
        **_base(),
        notice_type="general",
        content="notice",
    )
    redacted = legacy.model_copy(update={"raw": {"headers": {"cookie": "***"}}})

    assert redacted.raw == {"headers": {"cookie": "***"}}
    assert redacted.notice_type == legacy.notice_type
    assert legacy.raw == RAW_SECRET


def test_existing_parser_still_returns_legacy_protocol_dto_then_can_be_adapted() -> None:
    frame = protocol_events.WsFrame(
        body={
            "bizType": "text",
            "1": "hello",
            "2": "buyer-1",
            "3": "seller-1",
            "4": "text",
            "10": "chat-1",
        }
    )

    parsed = parse_frame(frame, "seller-1")

    assert len(parsed) == 1
    legacy = parsed[0]
    assert isinstance(legacy, protocol_events.MessageReceived)
    assert legacy.raw is not None

    mapped = to_domain_event(legacy)
    assert isinstance(mapped, domain_events.MessageReceived)
    assert mapped.content == "hello"
    assert "raw" not in mapped.model_dump()


def test_batch_mapper_preserves_order_without_protocol_instances() -> None:
    legacy_events = [event for event, _ in _legacy_events()]

    mapped = to_domain_events(legacy_events)

    assert [type(event).__name__ for event in mapped] == [
        "MessageReceived",
        "MessageSent",
        "OrderCreated",
        "OrderPaid",
        "OrderDelivered",
        "SystemNotice",
    ]
    assert all(not isinstance(event, protocol_events.EventEnvelope) for event in mapped)


def test_operational_protocol_events_are_not_domain_business_events() -> None:
    operational = protocol_events.ConnectionStateChanged(
        event_id="evt-state",
        account_id="seller-1",
        state=protocol_events.ConnectionState.CONNECTED,
    )

    with pytest.raises(TypeError, match="unsupported protocol event"):
        to_domain_event(cast(ProtocolDomainEvent, operational))
