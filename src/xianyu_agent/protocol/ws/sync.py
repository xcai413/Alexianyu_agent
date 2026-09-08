"""Canonical WebSocket sync-state protocol primitives."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws.request_router import RequestRouter

SyncFrame: TypeAlias = dict[str, Any]
SendRequest: TypeAlias = Callable[[SyncFrame], Awaitable[bool | None]]
MidFactory: TypeAlias = Callable[[], str]

GET_STATE_LWP = "/r/SyncStatus/getState"
ACK_DIFF_LWP = "/r/SyncStatus/ackDiff"
SYNC_TOPIC = "sync"
SYNC_EXTRA_TYPES = frozenset({1, 2})
DEFAULT_TIMEOUT_S = 30.0


class SyncStateError(RuntimeError):
    """The remote sync-state exchange violated the expected protocol contract."""


@dataclass(frozen=True, slots=True)
class SubscriptionReady:
    """Protocol marker produced only after a syncExtra getState/ackDiff exchange succeeds."""

    sync_type: int
    state_body: Any


def build_sync_frame(*, now_ms: int | None = None) -> dict:
    """Build the existing legacy `/r/SyncStatus/ackDiff` initial sync frame."""
    timestamp_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return {
        "lwp": ACK_DIFF_LWP,
        "headers": {"mid": _generate_mid()},
        "body": [
            {
                "pipeline": "sync",
                "tooLong2Tag": "PNM,1",
                "channel": "sync",
                "topic": "sync",
                "highPts": 0,
                "pts": timestamp_ms * 1000,
                "seq": 0,
                "timestamp": timestamp_ms,
            }
        ],
    }


def extract_sync_extra_type(frame: Any) -> int | None:
    """Return the normalized ``body.syncExtraType.type`` value when present."""
    if isinstance(frame, WsFrame):
        body = frame.body
    elif isinstance(frame, dict):
        body = frame.get("body")
    else:
        return None
    if not isinstance(body, dict):
        return None
    extra = body.get("syncExtraType")
    if not isinstance(extra, dict):
        return None
    return _protocol_code(extra.get("type"))


def requires_state_sync(frame: Any) -> bool:
    """Whether a sync-extra frame requires the getState → ackDiff exchange."""
    return extract_sync_extra_type(frame) in SYNC_EXTRA_TYPES


def build_get_state_request(*, mid_factory: MidFactory | None = None) -> SyncFrame:
    """Build the canonical `/r/SyncStatus/getState` request."""
    return _build_request(
        lwp=GET_STATE_LWP,
        body=[{"topic": SYNC_TOPIC}],
        mid_factory=mid_factory,
    )


def build_ack_diff_request(
    state_body: Any,
    *,
    mid_factory: MidFactory | None = None,
) -> SyncFrame:
    """Build `/r/SyncStatus/ackDiff` with the exact body returned by getState."""
    if state_body is None:
        msg = "getState body must not be None"
        raise ValueError(msg)
    return _build_request(
        lwp=ACK_DIFF_LWP,
        body=[state_body],
        mid_factory=mid_factory,
    )


async def handle_sync_extra(
    router: RequestRouter,
    send_request: SendRequest,
    frame: Any,
    *,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    mid_factory: MidFactory | None = None,
) -> SubscriptionReady | None:
    """Run the syncExtra getState → ackDiff protocol contract.

    Unsupported or malformed sync-extra frames are intentional no-ops. For
    protocol types 1 and 2, each request is registered with ``RequestRouter``
    before sending so concurrent exchanges remain isolated. This helper owns no
    transport receive loop and does not mutate runtime/worker state.
    """
    sync_type = extract_sync_extra_type(frame)
    if sync_type is None or sync_type not in SYNC_EXTRA_TYPES:
        return None

    state_response = await _request_response(
        router,
        send_request,
        build_get_state_request(mid_factory=mid_factory),
        timeout_s=timeout_s,
    )
    state = _require_response_mapping(state_response, operation="getState")
    state_code = _protocol_code(state.get("code"))
    state_body = state.get("body")
    if state_code != 200 or state_body is None:
        msg = f"getState response invalid: code={state.get('code')!r}"
        raise SyncStateError(msg)

    ack_response = await _request_response(
        router,
        send_request,
        build_ack_diff_request(state_body, mid_factory=mid_factory),
        timeout_s=timeout_s,
    )
    ack = _require_response_mapping(ack_response, operation="ackDiff")
    ack_code = _protocol_code(ack.get("code"))
    if ack_code is not None and ack_code != 200:
        msg = f"ackDiff response invalid: code={ack_code}"
        raise SyncStateError(msg)

    return SubscriptionReady(sync_type=sync_type, state_body=state_body)


async def _request_response(
    router: RequestRouter,
    send_request: SendRequest,
    frame: SyncFrame,
    *,
    timeout_s: float | None,
) -> Any:
    """Register-before-send and delegate waiter cleanup to ``RequestRouter``."""
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
        msg = f"WebSocket sync-state request send failed: {frame['lwp']}"
        raise ConnectionError(msg)
    return await router.wait(pending, timeout_s=timeout_s)


def _build_request(*, lwp: str, body: list[Any], mid_factory: MidFactory | None) -> SyncFrame:
    request_id = (mid_factory or _generate_mid)()
    if not request_id:
        msg = "mid factory returned an empty request id"
        raise ValueError(msg)
    return {"lwp": lwp, "headers": {"mid": request_id}, "body": body}


def _require_response_mapping(response: Any, *, operation: str) -> dict[str, Any]:
    """Accept either a raw mapping or the decoder's structural ``payload`` wrapper."""
    payload = getattr(response, "payload", response)
    if not isinstance(payload, dict):
        msg = f"{operation} response must be a mapping"
        raise SyncStateError(msg)
    return payload


def _protocol_code(value: Any) -> int | None:
    """Normalize the numeric/string code representations observed on the wire."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _generate_mid() -> str:
    """Preserve the legacy initial-sync message-id algorithm."""
    return f"{uuid.uuid4().int % 1000}{int(time.time() * 1000)} 0"
