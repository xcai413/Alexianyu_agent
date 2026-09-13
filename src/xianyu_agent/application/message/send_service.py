"""Canonical application boundary for outbound chat messages."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
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


class MessageAttemptRecorder(Protocol):
    """Append-only persistence port for external send attempt evidence."""

    async def record_attempt(
        self,
        *,
        attempt_id: str,
        account_id: str,
        chat_id: str,
        receiver_id: str,
        text: str,
    ) -> None: ...

    async def record_result(
        self,
        *,
        attempt_id: str,
        status: SendAttemptStatus,
        request_id: str | None,
        client_message_id: str | None,
        detail: str | None,
    ) -> None: ...


class MessageStore(Protocol):
    """Canonical conversation lookup and confirmed-outbound persistence port."""

    async def resolve_receiver(self, *, account_id: str, chat_id: str) -> str | None: ...

    async def record_outbound(
        self,
        *,
        account_id: str,
        chat_id: str,
        receiver_id: str,
        text: str,
        client_message_id: str | None,
        response: Any,
    ) -> None: ...


class SendMessageService:
    """Route outbound text through one correlated, no-blind-retry boundary."""

    def __init__(
        self,
        protocol: MessageProtocol,
        recorder: MessageAttemptRecorder,
        message_store: MessageStore | None = None,
    ) -> None:
        self._protocol = protocol
        self._recorder = recorder
        self._message_store = message_store

    async def send_text(
        self,
        *,
        account_id: str,
        chat_id: str,
        text: str,
        receiver_id: str | None = None,
    ) -> SendMessageResult:
        """Resolve routing, persist evidence, write once, and classify the outcome."""
        account = _required(account_id, "account_id")
        conversation = _required(chat_id, "chat_id")
        content = _required(text, "text", strip=False)
        receiver_result = await self._receiver(account, conversation, receiver_id)
        if isinstance(receiver_result, SendMessageResult):
            return receiver_result
        receiver = receiver_result
        attempt_id = uuid.uuid4().hex

        try:
            await self._recorder.record_attempt(
                attempt_id=attempt_id,
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                text=content,
            )
        except Exception as exc:
            return SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.FAILED_FINAL,
                retry_allowed=False,
                detail=f"attempt audit failed before send: {type(exc).__name__}: {exc}",
            )

        try:
            receipt = await self._protocol.send_text_message(
                chat_id=conversation,
                receiver_id=receiver,
                text=content,
            )
        except SendMessageNotSent as exc:
            result = self._failure_result(
                account=account,
                conversation=conversation,
                receiver=receiver,
                status=SendAttemptStatus.FAILED_RETRYABLE,
                retry_allowed=True,
                exc=exc,
            )
        except ConnectionError as exc:
            result = SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.FAILED_RETRYABLE,
                retry_allowed=True,
                detail=str(exc),
            )
        except SendMessageRejected as exc:
            result = self._failure_result(
                account=account,
                conversation=conversation,
                receiver=receiver,
                status=SendAttemptStatus.FAILED_FINAL,
                retry_allowed=False,
                exc=exc,
            )
        except SendMessageUncertain as exc:
            result = self._failure_result(
                account=account,
                conversation=conversation,
                receiver=receiver,
                status=SendAttemptStatus.UNCERTAIN,
                retry_allowed=False,
                exc=exc,
            )
        except Exception as exc:
            result = SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.UNCERTAIN,
                retry_allowed=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = SendMessageResult(
                account_id=account,
                chat_id=conversation,
                receiver_id=receiver,
                status=SendAttemptStatus.SUCCESS,
                retry_allowed=False,
                request_id=getattr(receipt, "request_id", None),
                client_message_id=getattr(receipt, "client_message_id", None),
                response=getattr(receipt, "response", receipt),
            )
            result = await self._persist_confirmed_outbound(result, content)

        return await self._record_terminal(attempt_id, result)

    async def _receiver(
        self,
        account: str,
        conversation: str,
        receiver_id: str | None,
    ) -> str | SendMessageResult:
        if receiver_id is not None and receiver_id.strip():
            return receiver_id.strip()
        if self._message_store is None:
            return self._preflight_failure(
                account,
                conversation,
                status=SendAttemptStatus.FAILED_FINAL,
                retry_allowed=False,
                detail="receiver_id missing and no canonical message store configured",
            )
        try:
            receiver = await self._message_store.resolve_receiver(
                account_id=account,
                chat_id=conversation,
            )
        except Exception as exc:
            return self._preflight_failure(
                account,
                conversation,
                status=SendAttemptStatus.FAILED_RETRYABLE,
                retry_allowed=True,
                detail=f"receiver lookup failed before send: {type(exc).__name__}: {exc}",
            )
        if receiver is None or not receiver.strip():
            return self._preflight_failure(
                account,
                conversation,
                status=SendAttemptStatus.FAILED_FINAL,
                retry_allowed=False,
                detail="no receiver found for conversation",
            )
        return receiver.strip()

    def _preflight_failure(
        self,
        account: str,
        conversation: str,
        *,
        status: SendAttemptStatus,
        retry_allowed: bool,
        detail: str,
    ) -> SendMessageResult:
        return SendMessageResult(
            account_id=account,
            chat_id=conversation,
            receiver_id="",
            status=status,
            retry_allowed=retry_allowed,
            detail=detail,
        )

    async def _persist_confirmed_outbound(
        self,
        result: SendMessageResult,
        content: str,
    ) -> SendMessageResult:
        if self._message_store is None:
            return result
        try:
            await self._message_store.record_outbound(
                account_id=result.account_id,
                chat_id=result.chat_id,
                receiver_id=result.receiver_id,
                text=content,
                client_message_id=result.client_message_id,
                response=result.response,
            )
        except Exception as exc:
            return replace(
                result,
                status=SendAttemptStatus.RECONCILIATION_REQUIRED,
                retry_allowed=False,
                detail=f"outbound persistence failed after platform success: {type(exc).__name__}: {exc}",
            )
        return result

    def _failure_result(
        self,
        *,
        account: str,
        conversation: str,
        receiver: str,
        status: SendAttemptStatus,
        retry_allowed: bool,
        exc: Exception,
    ) -> SendMessageResult:
        return SendMessageResult(
            account_id=account,
            chat_id=conversation,
            receiver_id=receiver,
            status=status,
            retry_allowed=retry_allowed,
            request_id=getattr(exc, "request_id", None),
            client_message_id=getattr(exc, "client_message_id", None),
            detail=str(exc),
        )

    async def _record_terminal(
        self,
        attempt_id: str,
        result: SendMessageResult,
    ) -> SendMessageResult:
        try:
            await self._recorder.record_result(
                attempt_id=attempt_id,
                status=result.status,
                request_id=result.request_id,
                client_message_id=result.client_message_id,
                detail=result.detail,
            )
        except Exception as exc:
            audit_detail = f"result audit failed: {type(exc).__name__}: {exc}"
            detail = f"{result.detail}; {audit_detail}" if result.detail else audit_detail
            if result.status is SendAttemptStatus.SUCCESS:
                return replace(
                    result,
                    status=SendAttemptStatus.RECONCILIATION_REQUIRED,
                    retry_allowed=False,
                    detail=detail,
                )
            return replace(result, detail=detail)
        return result


def _required(value: str, field: str, *, strip: bool = True) -> str:
    normalized = value.strip() if strip else value
    if not normalized or (not strip and not value.strip()):
        raise ValueError(f"{field} must not be empty")
    return normalized
