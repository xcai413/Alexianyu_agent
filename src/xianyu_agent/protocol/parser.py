"""WebSocket / mtop frame parser.

Converts raw WsFrame objects into one of the EventEnvelope subclasses.
All upstream field-number conventions are confined to this file; downstream
code never touches numbered keys directly.

Public surface:
    parse_frame(frame: WsFrame, account_id: str) -> list[EventEnvelope]
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from xianyu_agent.protocol.events import (
    ConnectionState,
    ConnectionStateChanged,
    ErrorOccurred,
    MessageContentType,
    MessageReceived,
    MessageSent,
    OrderCreated,
    OrderDelivered,
    OrderPaid,
    SystemNotice,
)
from xianyu_agent.protocol.ws import ack as ws_ack, normalizer as ws_normalizer

logger = logging.getLogger(__name__)

KEY_CONTENT = "1"
KEY_SENDER = "2"
KEY_RECEIVER = "3"
KEY_MSG_TYPE = "4"
KEY_EXTRA = "5"
KEY_TS_ID = "6"
KEY_CHAT_ID = "10"
KEY_ITEM = "100"

# Compatibility aliases while protocol helpers move under protocol/ws.
_decode_body = ws_normalizer.decode_body
_decode_sync_data = ws_normalizer.decode_sync_data
_fallback_mid = ws_ack.fallback_mid


def _make_envelope(account_id, raw):  # noqa: ARG001
    return uuid.uuid4().hex, datetime.now(UTC)


def build_ack_frame(frame) -> dict | None:
    """Legacy ACK builder entry delegated to the canonical WS ACK module."""
    return ws_ack.build_ack_frame(frame, mid_factory=_fallback_mid)


def unpack_sync_payloads(frame) -> list[dict]:
    """解包 `body.syncPushPackage.data[*].data`,支持 Base64 JSON/MessagePack。"""
    body = _decode_body(frame.body)
    if not isinstance(body, dict):
        return []
    package = body.get("syncPushPackage")
    if not isinstance(package, dict) or not isinstance(package.get("data"), list):
        return []
    payloads = []
    for entry in package["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), str):
            continue
        decoded = _decode_sync_data(entry["data"])
        if isinstance(decoded, dict):
            payloads.append(decoded)
    return payloads


def _truncate_raw(raw, *, limit=4096):
    if raw is None:
        return None
    try:
        as_str = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return None
    if len(as_str) <= limit:
        return raw
    return {"_truncated": as_str[:limit]}


def _parse_message(payload, *, account_id, direction, raw):
    content = str(payload.get(KEY_CONTENT, "") or "")
    if not content:
        return None
    type_str = str(payload.get(KEY_MSG_TYPE, "text"))
    try:
        content_type = MessageContentType(type_str)
    except ValueError:
        content_type = MessageContentType.TEXT
    ts_id = payload.get(KEY_TS_ID) or {}
    event_id, received_at = _make_envelope(account_id, raw)
    chat_id = str(payload.get(KEY_CHAT_ID) or "")
    if not chat_id:
        return None
    common = {
        "event_id": event_id,
        "account_id": account_id,
        "received_at": received_at,
        "raw": _truncate_raw(raw),
        "chat_id": chat_id,
        "message_id": str(ts_id.get("mid"))
        if isinstance(ts_id, dict) and ts_id.get("mid")
        else None,
        "content_type": content_type,
        "content": content,
        "item_id": _extract_item_id(payload),
        "sent_at": _timestamp_from_ms(ts_id.get("time")) if isinstance(ts_id, dict) else None,
    }
    extra = payload.get(KEY_EXTRA)
    image_url = None
    if isinstance(extra, dict) and extra.get("url"):
        image_url = str(extra.get("url"))
    if direction == "inbound":
        sender = str(payload.get(KEY_SENDER) or "")
        return MessageReceived(
            **common,
            sender_id=sender or "unknown",
            sender_name=str(payload.get("senderName", "") or "") or None,
            image_url=image_url,
        )
    receiver = str(payload.get(KEY_RECEIVER) or "")
    return MessageSent(**common, receiver_id=receiver or "unknown")


def _parse_live_message(payload, *, account_id, account_user_id, raw):
    """解析闲鱼 syncPushPackage 内的标准聊天结构。"""
    message_1 = payload.get("1")
    if not isinstance(message_1, dict):
        return None
    meta = message_1.get("10")
    if not isinstance(meta, dict):
        return None
    content = str(meta.get("reminderContent") or "")
    chat_id = _strip_domain(message_1.get("2"))
    sender_id = str(meta.get("senderUserId") or "")
    if not content or not chat_id or not sender_id:
        return None
    if _is_system_tip(meta):
        event_id, received_at = _make_envelope(account_id, raw)
        return SystemNotice(
            event_id=event_id,
            account_id=account_id,
            received_at=received_at,
            raw=_truncate_raw(raw),
            notice_type="system_tip",
            content=content,
        )

    event_id, received_at = _make_envelope(account_id, raw)
    common = {
        "event_id": event_id,
        "account_id": account_id,
        "received_at": received_at,
        "raw": _truncate_raw(raw),
        "chat_id": chat_id,
        "message_id": _extract_message_id(meta),
        "item_id": _extract_item_id(meta),
        "sent_at": _timestamp_from_ms(message_1.get("5")),
        "content_type": _content_type(content),
        "content": content,
    }
    if account_user_id and sender_id == account_user_id:
        return MessageSent(**common, receiver_id="unknown")
    return MessageReceived(
        **common,
        sender_id=sender_id,
        sender_name=str(meta.get("senderNick") or meta.get("reminderTitle") or "") or None,
    )


def _parse_order(payload, *, account_id, raw, kind):
    extra = payload.get(KEY_EXTRA)
    if not isinstance(extra, dict):
        extra = {}
    item_block = payload.get(KEY_ITEM)
    item_id = None
    item_title = None
    if isinstance(item_block, dict):
        item_id = str(item_block.get("itemId") or "") or None
        item_title = str(item_block.get("title") or "") or None
    order_id = str(extra.get("orderId") or extra.get("bizOrderId") or "") or None
    if not order_id:
        return None
    buyer_id = str(extra.get("buyerId") or "") or None
    buyer_name = str(extra.get("buyerNick") or "") or None
    amount = float(extra.get("amount") or 0)
    event_id, received_at = _make_envelope(account_id, raw)
    base = {
        "event_id": event_id,
        "account_id": account_id,
        "received_at": received_at,
        "raw": _truncate_raw(raw),
        "order_id": order_id,
        "item_id": item_id,
        "item_title": item_title,
        "buyer_id": buyer_id or "unknown",
        "buyer_name": buyer_name,
        "amount": amount,
    }
    if kind == "created":
        return OrderCreated(**base)
    if kind == "paid":
        ts_id = payload.get(KEY_TS_ID) or {}
        paid_at_ms = ts_id.get("time") if isinstance(ts_id, dict) else None
        paid_at = datetime.fromtimestamp(int(paid_at_ms) / 1000, tz=UTC) if paid_at_ms else None
        return OrderPaid(**base, paid_at=paid_at)
    if kind == "delivered":
        ts_id = payload.get(KEY_TS_ID) or {}
        delivered_ms = ts_id.get("time") if isinstance(ts_id, dict) else None
        delivered_at = (
            datetime.fromtimestamp(int(delivered_ms) / 1000, tz=UTC) if delivered_ms else None
        )
        return OrderDelivered(
            event_id=event_id,
            account_id=account_id,
            received_at=received_at,
            raw=_truncate_raw(raw),
            order_id=order_id,
            delivered_at=delivered_at,
        )
    return None


def _parse_system(payload, *, account_id, raw):
    content = str(payload.get(KEY_CONTENT, "") or "")
    if not content:
        return None
    event_id, received_at = _make_envelope(account_id, raw)
    extra = payload.get(KEY_EXTRA)
    notice_type = "general"
    if isinstance(extra, dict):
        notice_type = str(extra.get("type") or extra.get("noticeType") or "general")
    return SystemNotice(
        event_id=event_id,
        account_id=account_id,
        received_at=received_at,
        raw=_truncate_raw(raw),
        notice_type=notice_type,
        content=content,
    )


def parse_frame(  # noqa: PLR0912
    frame, account_id, *, account_user_id: str | None = None
):
    sync_payloads = unpack_sync_payloads(frame)
    if sync_payloads:
        events = []
        for payload in sync_payloads:
            raw = {"code": frame.code, "headers": frame.headers, "body": payload}
            event = _parse_live_message(
                payload,
                account_id=account_id,
                account_user_id=account_user_id,
                raw=raw,
            )
            if event is not None:
                events.append(event)
        return events
    body = _decode_body(frame.body)
    if body is None:
        logger.debug("skipping frame with undecodable body (code=%s)", frame.code)
        return []
    biz = str(body.get("bizType") or body.get("type") or "").lower()
    raw = {"code": frame.code, "headers": frame.headers, "body": body}
    events = []
    if biz in {"system", "notice"} or (KEY_MSG_TYPE in body and body.get(KEY_MSG_TYPE) == "system"):
        ev = _parse_system(body, account_id=account_id, raw=raw)
        if ev:
            events.append(ev)
        return events
    if biz == "order":
        extra = body.get(KEY_EXTRA)
        status = ""
        if isinstance(extra, dict):
            status = str(extra.get("status") or extra.get("orderStatus") or "").lower()
        kind = "created"
        if "paid" in status or "payed" in status or "已付款" in status:
            kind = "paid"
        elif "delivered" in status or "shipped" in status or "已发货" in status:
            kind = "delivered"
        ev = _parse_order(body, account_id=account_id, raw=raw, kind=kind)
        if ev:
            events.append(ev)
        return events
    if biz in {"text", "image", "card", "product", "1", "2"} or KEY_CONTENT in body:
        sender = str(body.get(KEY_SENDER) or "")
        direction = "inbound" if sender and sender != account_id else "outbound"
        ev = _parse_message(body, account_id=account_id, direction=direction, raw=raw)
        if ev:
            events.append(ev)
        return events
    logger.debug("unhandled bizType=%r keys=%s", biz, list(body.keys())[:10])
    return events


def _extract_json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _extract_message_id(meta: dict) -> str | None:
    for field in ("bizTag", "extJson"):
        value = _extract_json_object(meta.get(field))
        if value.get("messageId"):
            return str(value["messageId"])
    return None


def _extract_item_id(meta: dict) -> str | None:
    url = str(meta.get("reminderUrl") or "")
    if url:
        query = parse_qs(urlparse(url).query)
        if query.get("itemId"):
            return str(query["itemId"][0])
    for field in ("bizTag", "extJson"):
        value = _extract_json_object(meta.get(field))
        if value.get("itemId"):
            return str(value["itemId"])
    return None


def _timestamp_from_ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC) if value else None
    except (TypeError, ValueError, OSError):
        return None


def _strip_domain(value) -> str:
    return str(value or "").split("@", 1)[0]


def _content_type(content: str) -> MessageContentType:
    return MessageContentType.CARD if content == "[卡片消息]" else MessageContentType.TEXT


def _is_system_tip(meta: dict) -> bool:
    ext = _extract_json_object(meta.get("extJson"))
    return str(ext.get("msgArg1") or "") == "MsgTips"


def parse_error(account_id, code, message):
    return ErrorOccurred(
        event_id=uuid.uuid4().hex,
        account_id=account_id,
        code=code,
        message=message,
    )


def parse_state_change(account_id, state, detail=None):
    try:
        st = ConnectionState(state)
    except ValueError:
        st = ConnectionState.ERROR
    return ConnectionStateChanged(
        event_id=uuid.uuid4().hex,
        account_id=account_id,
        state=st,
        detail=detail,
    )
