"""WebSocket / mtop frame parser.

Converts raw WsFrame objects into one of the EventEnvelope subclasses.
All upstream field-number conventions are confined to this file; downstream
code never touches numbered keys directly.

Public surface:
    parse_frame(frame: WsFrame, account_id: str) -> list[EventEnvelope]
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime

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

logger = logging.getLogger(__name__)

KEY_CONTENT = "1"
KEY_SENDER = "2"
KEY_RECEIVER = "3"
KEY_MSG_TYPE = "4"
KEY_EXTRA = "5"
KEY_TS_ID = "6"
KEY_CHAT_ID = "10"
KEY_ITEM = "100"


def _decode_body(body):
    if body is None:
        return None
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        return None
    try:
        result = json.loads(body)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        decoded = base64.b64decode(body, validate=True)
        result = json.loads(decoded)
        if isinstance(result, dict):
            return result
    except (ValueError, json.JSONDecodeError):
        pass
    return None


def _make_envelope(account_id, raw):  # noqa: ARG001
    return uuid.uuid4().hex, datetime.now(UTC)


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


def parse_frame(frame, account_id):
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
