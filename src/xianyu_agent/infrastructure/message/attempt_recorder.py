"""Append-only AuditLog adapter for outbound message side-effect evidence."""

from __future__ import annotations

import hashlib

from xianyu_agent.application.message.send_service import SendAttemptStatus
from xianyu_agent.db import AuditLog, get_async_session
from xianyu_agent.db.models import AuditActor


class AuditLogMessageAttemptRecorder:
    """Persist pre-send and terminal evidence without storing message plaintext."""

    async def record_attempt(
        self,
        *,
        attempt_id: str,
        account_id: str,
        chat_id: str,
        receiver_id: str,
        text: str,
    ) -> None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with get_async_session() as session:
            session.add(
                AuditLog(
                    actor=AuditActor.SYSTEM.value,
                    action="message.send.attempt",
                    target=attempt_id,
                    params={
                        "attempt_id": attempt_id,
                        "account_id": account_id,
                        "chat_id": chat_id,
                        "receiver_id": receiver_id,
                        "content_sha256": digest,
                        "content_length": len(text),
                    },
                    result="ATTEMPTED",
                )
            )
            await session.commit()

    async def record_result(
        self,
        *,
        attempt_id: str,
        status: SendAttemptStatus,
        request_id: str | None,
        client_message_id: str | None,
        detail: str | None,
    ) -> None:
        async with get_async_session() as session:
            session.add(
                AuditLog(
                    actor=AuditActor.SYSTEM.value,
                    action="message.send.result",
                    target=attempt_id,
                    params={
                        "attempt_id": attempt_id,
                        "request_id": request_id,
                        "client_message_id": client_message_id,
                    },
                    result=status.value,
                    error=detail,
                )
            )
            await session.commit()
