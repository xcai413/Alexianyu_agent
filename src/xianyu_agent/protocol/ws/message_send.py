"""Calibrated Goofish WebSocket text-message request/response primitive."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

from xianyu_agent.protocol.ws.request_router import RequestRouter

MessageSendFrame: TypeAlias = dict[str, Any]
SendRequest: TypeAlias = Callable[[MessageSendFrame], Awaitable[bool | None]]
MidFactory: TypeAlias = Callable[[], str]
UuidFactory: TypeAlias = Callable[[], str]

MESSAGE_SEND_LWP = "/r/MessageSend/sendByReceiverScope"
DEFAULT_TIMEOUT_S = 30.0


class MessageSendError(RuntimeError):
    """Base error for one outbound message attempt with correlation evidence."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        client_message_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.client_message_id = client_message_id


class MessageSendNotSent(MessageSendError):
    """The transport proved that the request was not written."""


class MessageSendUncertain(MessageSendError):
    """The request may have reached the platform, so blind retry is unsafe."""


class MessageSendRejected(MessageSendError):
    """The platform returned an explicit non-success response."""


@dataclass(frozen=True, slots=True)
class MessageSendReceipt:
    """Correlated successful response for one outbound message request."""

    request_id: str
    client_message_id: str
    response: Any


def generate_mid() -> str:
    """Generate a request id using the existing DingTalk/Goofish M5 convention."""
    return f"{uuid.uuid4().int % 1000}{int(time.time() * 1000)} 0"


def generate_client_message_id() -> str:
    """Generate the calibrated web-client message uuid shape."""
    return f"-{int(time.time() * 1000)}1"


def build_text_message_request(
    *,
    account_user_id: str,
    chat_id: str,
    receiver_id: str,
    text: str,
    mid_factory: MidFactory | None = None,
    uuid_factory: UuidFactory | None = None,
) -> MessageSendFrame:
    """Build one calibrated ``sendByReceiverScope`` text-message frame."""
    seller = _required(account_user_id, "account_user_id")
    conversation = _required(chat_id, "chat_id")
    receiver = _required(receiver_id, "receiver_id")
    content_text = _required(text, "text", strip=False)
    request_id = _required((mid_factory or generate_mid)(), "request_id")
    client_message_id = _required(
        (uuid_factory or generate_client_message_id)(),
        "client_message_id",
    )

    payload = {
        "contentType": 1,
        "text": {"text": content_text},
    }
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    return {
        "lwp": MESSAGE_SEND_LWP,
        "headers": {"mid": request_id},
        "body": [
            {
                "uuid": client_message_id,
                "cid": f"{conversation}@goofish",
                "conversationType": 1,
                "content": {
                    "contentType": 101,
                    "custom": {
                        "type": 1,
                        "data": encoded,
                    },
                },
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {
                    "appVersion": "1.0",
                    "platform": "web",
                },
                "mtags": {},
                "msgReadStatusSetting": 1,
            },
            {
                "actualReceivers": [
                    f"{receiver}@goofish",
                    f"{seller}@goofish",
                ]
            },
        ],
    }


async def request_text_message(
    router: RequestRouter,
    send_request: SendRequest,
    *,
    account_user_id: str,
    chat_id: str,
    receiver_id: str,
    text: str,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    mid_factory: MidFactory | None = None,
    uuid_factory: UuidFactory | None = None,
) -> MessageSendReceipt:
    """Write once and await the correlated platform response.

    ``False`` from ``send_request`` is reserved for an adapter that can prove no
    bytes were written. Any exception during the write, or any timeout/cancelled
    response after a successful write, is fail-closed as
    :class:`MessageSendUncertain` because the platform side effect cannot safely
    be excluded. Caller task cancellation still propagates unchanged.
    """
    frame = build_text_message_request(
        account_user_id=account_user_id,
        chat_id=chat_id,
        receiver_id=receiver_id,
        text=text,
        mid_factory=mid_factory,
        uuid_factory=uuid_factory,
    )
    request_id = str(frame["headers"]["mid"])
    client_message_id = str(frame["body"][0]["uuid"])
    pending = router.register(request_id)

    try:
        sent = await send_request(frame)
    except asyncio.CancelledError:
        router.cancel(request_id)
        raise
    except Exception as exc:
        router.cancel(request_id)
        msg = f"message write outcome uncertain: {type(exc).__name__}: {exc}"
        raise MessageSendUncertain(
            msg,
            request_id=request_id,
            client_message_id=client_message_id,
        ) from exc

    if sent is False:
        router.cancel(request_id)
        raise MessageSendNotSent(
            "message request was not written",
            request_id=request_id,
            client_message_id=client_message_id,
        )

    try:
        correlated = await router.wait(pending, timeout_s=timeout_s)
    except asyncio.CancelledError as exc:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        raise MessageSendUncertain(
            "message response cancelled after write",
            request_id=request_id,
            client_message_id=client_message_id,
        ) from exc
    except TimeoutError as exc:
        raise MessageSendUncertain(
            "message response timed out after write",
            request_id=request_id,
            client_message_id=client_message_id,
        ) from exc

    response = _response_payload(correlated)
    code = _response_code(response)
    if code is None:
        raise MessageSendUncertain(
            "correlated message response has no explicit status code",
            request_id=request_id,
            client_message_id=client_message_id,
        )
    if code != 200:
        raise MessageSendRejected(
            f"message request rejected with code={code}",
            request_id=request_id,
            client_message_id=client_message_id,
        )

    return MessageSendReceipt(
        request_id=request_id,
        client_message_id=client_message_id,
        response=response,
    )


def _response_payload(response: Any) -> Any:
    """Normalize RequestRouter's live ``DecodedFrame`` response to its wire payload."""
    payload = getattr(response, "payload", None)
    return payload if payload is not None else response


def _response_code(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    value = response.get("code")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _required(value: str, field: str, *, strip: bool = True) -> str:
    normalized = value.strip() if strip else value
    if not normalized or (not strip and not value.strip()):
        raise ValueError(f"{field} must not be empty")
    return normalized
