"""Unit tests for protocol layer (events, parser, signer)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime

import msgpack
import pytest

from xianyu_agent.protocol.events import (
    ConnectionState,
    MessageContentType,
    MessageDirection,
    OrderCreated,
    OrderDelivered,
    WsFrame,
)
from xianyu_agent.protocol.parser import (
    _decode_body,
    build_ack_frame,
    parse_error,
    parse_frame,
    parse_state_change,
    unpack_sync_payloads,
)
from xianyu_agent.protocol.signer import (
    APP_KEY,
    MtopHeaders,
    compute_sign,
    derive_token_seed,
    make_headers,
)


def test_ws_frame_from_dict() -> None:
    raw = {
        "code": 0,
        "packetId": "abc-123",
        "headers": {"appKey": APP_KEY},
        "body": {"bizType": "text", "1": "hi"},
    }
    frame = WsFrame.model_validate(raw)
    assert frame.code == 0
    assert frame.packet_id == "abc-123"
    assert frame.body == {"bizType": "text", "1": "hi"}
    assert isinstance(frame.received_at, datetime)


def test_ws_frame_handles_string_body() -> None:
    raw = {"code": 0, "body": '{"bizType": "text"}'}
    frame = WsFrame.model_validate(raw)
    assert isinstance(frame.body, str)
    decoded = _decode_body(frame.body)
    assert isinstance(decoded, dict)
    assert decoded["bizType"] == "text"


def test_parser_text_message_inbound() -> None:
    raw = WsFrame(
        body={
            "bizType": "text",
            "1": "在吗?",
            "2": "buyer-1",
            "3": "seller-1",
            "4": "text",
            "6": {"mid": "m-1", "time": 1700000000000},
            "10": "chat-1",
        }
    )
    events = parse_frame(raw, "seller-1")
    assert len(events) == 1
    e = events[0]
    assert e.chat_id == "chat-1"
    assert e.content == "在吗?"
    assert e.sender_id == "buyer-1"
    assert e.direction == MessageDirection.INBOUND
    assert e.content_type == MessageContentType.TEXT
    assert e.message_id == "m-1"


def test_parser_message_outbound_routed_by_sender() -> None:
    raw = WsFrame(
        body={
            "bizType": "text",
            "1": "OK",
            "2": "seller-1",
            "3": "buyer-1",
            "4": "text",
            "6": {"mid": "m-2"},
            "10": "chat-2",
        }
    )
    events = parse_frame(raw, "seller-1")
    assert len(events) == 1
    assert events[0].direction == MessageDirection.OUTBOUND


def test_parser_skips_empty_message() -> None:
    raw = WsFrame(body={"bizType": "text", "1": "", "10": "chat-x"})
    events = parse_frame(raw, "seller-1")
    assert events == []


def test_parser_skips_message_without_chat_id() -> None:
    raw = WsFrame(body={"bizType": "text", "1": "hello"})
    events = parse_frame(raw, "seller-1")
    assert events == []


def test_parser_handles_base64_body() -> None:
    payload = {"bizType": "text", "1": "from base64", "2": "buyer-x", "10": "chat-b64"}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    raw = WsFrame(body=encoded)
    events = parse_frame(raw, "seller-1")
    assert len(events) == 1
    assert events[0].content == "from base64"


def _sync_frame(payload: dict, *, use_msgpack: bool = False) -> WsFrame:
    raw = (
        msgpack.packb(payload, use_bin_type=True)
        if use_msgpack
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    encoded = base64.b64encode(raw).decode("ascii")
    return WsFrame(
        headers={"mid": "push-mid", "sid": "push-sid", "app-key": "app"},
        body={"syncPushPackage": {"data": [{"data": encoded}]}},
    )


def test_sync_push_json_maps_real_chat_fields() -> None:
    payload = {
        "1": {
            "2": "chat-real@goofish",
            "5": 1700000000000,
            "10": {
                "senderUserId": "buyer-real",
                "senderNick": "真实买家",
                "reminderContent": "真实问价",
                "reminderUrl": "https://www.goofish.com/item?id=x&itemId=ITEM-9",
                "bizTag": '{"messageId":"MSG-9"}',
            },
        }
    }
    events = parse_frame(_sync_frame(payload), "alias", account_user_id="seller-unb")
    assert len(events) == 1
    event = events[0]
    assert event.chat_id == "chat-real"
    assert event.message_id == "MSG-9"
    assert event.item_id == "ITEM-9"
    assert event.sender_id == "buyer-real"
    assert event.sender_name == "真实买家"
    assert event.content == "真实问价"
    assert event.sent_at is not None
    assert event.received_at >= event.sent_at


def test_sync_push_msgpack_and_outbound_direction() -> None:
    payload = {
        "1": {
            "2": "chat-msgpack@goofish",
            "10": {
                "senderUserId": "seller-unb",
                "reminderContent": "卖家回复",
                "extJson": '{"messageId":"MSG-MP","itemId":"ITEM-MP"}',
            },
        }
    }
    frame = _sync_frame(payload, use_msgpack=True)
    assert unpack_sync_payloads(frame) == [payload]
    events = parse_frame(frame, "alias", account_user_id="seller-unb")
    assert len(events) == 1
    assert events[0].direction == MessageDirection.OUTBOUND
    assert events[0].message_id == "MSG-MP"


def test_sync_system_tip_is_not_buyer_message() -> None:
    payload = {
        "1": {
            "2": "chat-tip@goofish",
            "10": {
                "senderUserId": "platform",
                "reminderContent": "活动提醒",
                "extJson": '{"msgArg1":"MsgTips"}',
            },
        }
    }
    events = parse_frame(_sync_frame(payload), "alias", account_user_id="seller-unb")
    assert len(events) == 1
    assert events[0].notice_type == "system_tip"


def test_push_ack_preserves_correlation_headers() -> None:
    frame = _sync_frame({"1": {}})
    ack = build_ack_frame(frame)
    assert ack == {
        "code": 200,
        "headers": {"mid": "push-mid", "sid": "push-sid", "app-key": "app"},
    }
    assert build_ack_frame(WsFrame(code=200, headers={"mid": "x"})) is None
    push_with_code_200 = _sync_frame({"1": {}})
    push_with_code_200.code = 200
    assert build_ack_frame(push_with_code_200) is not None


def test_parser_order_paid() -> None:
    raw = WsFrame(
        body={
            "bizType": "order",
            "5": {
                "orderId": "O-100",
                "buyerId": "buyer-1",
                "buyerNick": "买家昵称",
                "amount": 9.9,
                "status": "paid",
            },
            "100": {"itemId": "I-1", "title": "好物"},
            "6": {"time": 1700000000000},
        }
    )
    events = parse_frame(raw, "seller-1")
    assert len(events) == 1
    e = events[0]
    assert e.order_id == "O-100"
    assert e.item_id == "I-1"
    assert e.item_title == "好物"
    assert e.amount == 9.9
    assert e.buyer_id == "buyer-1"
    assert e.buyer_name == "买家昵称"
    assert e.paid_at is not None
    assert e.paid_at.year == 2023


def test_parser_order_created_when_status_missing() -> None:
    raw = WsFrame(
        body={
            "bizType": "order",
            "5": {"orderId": "O-200", "buyerId": "buyer-2", "amount": 1.0},
        }
    )
    events = parse_frame(raw, "seller-1")
    assert len(events) == 1
    assert isinstance(events[0], OrderCreated)


def test_parser_order_delivered_via_chinese_status() -> None:
    raw = WsFrame(
        body={
            "bizType": "order",
            "5": {"orderId": "O-300", "status": "已发货"},
        }
    )
    events = parse_frame(raw, "seller-1")
    assert len(events) == 1
    assert isinstance(events[0], OrderDelivered)


def test_parser_skips_order_without_order_id() -> None:
    raw = WsFrame(body={"bizType": "order", "5": {"amount": 1.0}})
    events = parse_frame(raw, "seller-1")
    assert events == []


def test_parser_system_notice() -> None:
    raw = WsFrame(
        body={
            "bizType": "system",
            "1": "账号在异地登录",
            "5": {"type": "risk"},
        }
    )
    events = parse_frame(raw, "seller-1")
    assert len(events) == 1
    assert events[0].notice_type == "risk"
    assert events[0].content == "账号在异地登录"


def test_parser_returns_empty_on_undecodable_body() -> None:
    raw = WsFrame(body="not base64 and not json {}")
    events = parse_frame(raw, "seller-1")
    assert events == []


def test_parse_error_and_state_helpers() -> None:
    err = parse_error("acc-1", "AUTH", "missing token")
    assert err.code == "AUTH"
    assert err.account_id == "acc-1"
    state = parse_state_change("acc-1", "reconnecting", "5 retries")
    assert state.state == ConnectionState.RECONNECTING
    assert state.detail == "5 retries"
    bad = parse_state_change("acc-1", "not-a-state")
    assert bad.state == ConnectionState.ERROR


def test_derive_token_seed_splits_on_underscore() -> None:
    assert derive_token_seed("abc123_def456") == "abc123"
    assert derive_token_seed("no-underscore") == "no-underscore"


def test_derive_token_seed_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        derive_token_seed("")


def test_compute_sign_is_stable_md5() -> None:
    expected = hashlib.md5(b"seed&1700000000000&34839810&data").hexdigest()
    assert compute_sign("seed", 1700000000000, "34839810", "data") == expected


def test_make_headers_has_required_fields() -> None:
    h = make_headers("seed_xyz", data="hello")
    assert isinstance(h, MtopHeaders)
    assert h.app_key == APP_KEY
    assert h.timestamp_ms > 0
    assert len(h.sign) == 32
    assert h.token == "seed_xyz"


def test_make_headers_sign_changes_with_data() -> None:
    h1 = make_headers("seed_xyz", data="hello")
    h2 = make_headers("seed_xyz", data="world")
    assert h1.sign != h2.sign
