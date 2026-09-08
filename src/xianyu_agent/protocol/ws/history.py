"""Conversation and message-history WebSocket request/response primitives."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from xianyu_agent.protocol.ws.request_router import RequestRouter

HistoryFrame: TypeAlias = dict[str, Any]
SendRequest: TypeAlias = Callable[[HistoryFrame], Awaitable[bool | None]]
MidFactory: TypeAlias = Callable[[], str]

CONVERSATION_LIST_LWP = "/r/Conversation/listNewestPagination"
MESSAGE_HISTORY_LWP = "/r/MessageManager/listUserMessages"
NEWEST_CURSOR = 9_007_199_254_740_991
MAX_PAGE_SIZE = 100
DEFAULT_CONVERSATION_LIMIT = 100
DEFAULT_MESSAGE_LIMIT = 50
DEFAULT_TIMEOUT_S = 30.0


def build_conversation_list_request(
    *,
    cursor: int = 0,
    limit: int = 0,
    mid_factory: MidFactory | None = None,
) -> HistoryFrame:
    """Build one official IM conversation-list pagination request."""
    return _build_request(
        lwp=CONVERSATION_LIST_LWP,
        body=[_normalize_cursor(cursor), _normalize_limit(limit, DEFAULT_CONVERSATION_LIMIT)],
        mid_factory=mid_factory,
    )


def build_message_history_request(
    cid: str,
    *,
    cursor: int = 0,
    limit: int = 0,
    mid_factory: MidFactory | None = None,
) -> HistoryFrame:
    """Build one official IM message-history pagination request."""
    conversation_id = cid.strip()
    if not conversation_id:
        msg = "conversation id must not be empty"
        raise ValueError(msg)
    return _build_request(
        lwp=MESSAGE_HISTORY_LWP,
        body=[
            conversation_id,
            False,
            _normalize_cursor(cursor),
            _normalize_limit(limit, DEFAULT_MESSAGE_LIMIT),
            False,
        ],
        mid_factory=mid_factory,
    )


async def request_conversations(
    router: RequestRouter,
    send_request: SendRequest,
    *,
    cursor: int = 0,
    limit: int = 0,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    mid_factory: MidFactory | None = None,
) -> Any:
    """Send and await one conversation-list response through ``router``."""
    frame = build_conversation_list_request(
        cursor=cursor,
        limit=limit,
        mid_factory=mid_factory,
    )
    return await _request_response(router, send_request, frame, timeout_s=timeout_s)


async def request_message_history(
    router: RequestRouter,
    send_request: SendRequest,
    cid: str,
    *,
    cursor: int = 0,
    limit: int = 0,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    mid_factory: MidFactory | None = None,
) -> Any:
    """Send and await one conversation's message-history response through ``router``."""
    frame = build_message_history_request(
        cid,
        cursor=cursor,
        limit=limit,
        mid_factory=mid_factory,
    )
    return await _request_response(router, send_request, frame, timeout_s=timeout_s)


async def _request_response(
    router: RequestRouter,
    send_request: SendRequest,
    frame: HistoryFrame,
    *,
    timeout_s: float | None,
) -> Any:
    """Register-before-send and delegate rendezvous cleanup to ``RequestRouter``."""
    request_id = str(frame["headers"]["mid"])
    pending = router.register(request_id)
    try:
        sent = await send_request(frame)
    except asyncio.CancelledError:
        router.cancel(request_id)
        raise
    except Exception:
        router.cancel(request_id)
        raise
    if sent is False:
        router.cancel(request_id)
        msg = f"WebSocket history request send failed: {frame['lwp']}"
        raise ConnectionError(msg)
    return await router.wait(pending, timeout_s=timeout_s)


def generate_mid() -> str:
    """Generate a protocol request id using the existing M5 mid convention."""
    return f"{uuid.uuid4().int % 1000}{int(time.time() * 1000)} 0"


def _build_request(*, lwp: str, body: list[Any], mid_factory: MidFactory | None) -> HistoryFrame:
    request_id = (mid_factory or generate_mid)()
    if not request_id:
        msg = "mid factory returned an empty request id"
        raise ValueError(msg)
    return {"lwp": lwp, "headers": {"mid": request_id}, "body": body}


def _normalize_cursor(cursor: int) -> int:
    return cursor if cursor > 0 else NEWEST_CURSOR


def _normalize_limit(limit: int, default: int) -> int:
    if limit <= 0 or limit > MAX_PAGE_SIZE:
        return default
    return limit
