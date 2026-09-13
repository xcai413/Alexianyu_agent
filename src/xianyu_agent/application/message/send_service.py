"""Canonical application boundary for outbound chat messages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from xianyu_agent.protocol.ws import message_send

SendMessageNotSent = message_send.MessageSendNotSent
SendMessageUncertain = message_send.MessageSendUncertain
SendMessageRejected = message_send.MessageSendRejected


class SendAttemptStatus(StrEnum):
    """Cross-domain external-side-effect result vocabulary."""

    SUCCESS = "SUCCESS"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    UNCERTAIN = "UNCERTAIN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class SendMessageResult:
    """One application-level send attempt outcome."""

    account_id: str
    chat_id: str
    receiver_id: str
    status: SendAttemptStatus
    retry_allowed: bool
    request_id: str | None = None
    client_message_id: str | None = None
    detail: str | None = None
    response: Any = None


class MessageProtocol(Protocol):
    """Business-facing protocol capability required by the send service."""

    async def send_text_message(
        self,
        *,
        chat_id: str,
        receiver_id: str,
        text: str,
    ) -> Any: ...


class SendMessageService:
    """Route every outbound text message through one no-blind-retry boundary."""

    def __init__(self, protocol: MessageProtocol) -> None:
        self._protocol = protocol

    async def send_text(
        self,
        *,
        account_id: str,
        chat_id: str,
        receiver_id: str,
        text: str,
    ) -> SendMessageResult:
        """Perform exactly one protocol attempt and classify its outcome."""
        account = _required(account_id, "account_id")
        conversation = _required(chat_id, "chat_id")
        receiver = _required(receiver_id, "receiver_id")
        content = _required(text, "text", strip=False)

        try:
            receipt = await self._protocol.send_text_message(
                chat_id=conversation,
                receiver_id=receiver,
                text=content,
            )
        except SendMessageNotSent as exc:
            return SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.FAILED_RETRYABLE,
                retry_allowed=True,
                detail=str(exc),
            )
        except SendMessageRejected as exc:
            return SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.FAILED_FINAL,
                retry_allowed=False,
                detail=str(exc),
            )
        except SendMessageUncertain as exc:
            return SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.UNCERTAIN,
                retry_allowed=False,
                detail=str(exc),
            )
        except Exception as exc:
            # An unknown adapter failure cannot prove the platform side effect did
            # not happen. Fail closed rather than introducing an implicit retry.
            return SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.UNCERTAIN,
                retry_allowed=False,
                detail=f"{type(exc).__name__}: {exc}",
            )

        return SendMessageResult(
            account_id=account,
            chat_id=conversation,
            receiver_id=receiver,
            status=SendAttemptStatus.SUCCESS,
            retry_allowed=False,
            request_id=getattr(receipt, "request_id", None),
            client_message_id=getattr(receipt, "client_message_id", None),
            response=getattr(receipt, "response", receipt),
        )


def _required(value: str, field: str, *, strip: bool = True) -> str:
    normalized = value.strip() if strip else value
    if not normalized or (not strip and not value.strip()):
        raise ValueError(f"{field} must not be empty")
    return normalized
