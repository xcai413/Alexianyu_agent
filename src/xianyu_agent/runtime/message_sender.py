"""Runtime composition for canonical outbound message sending."""

from __future__ import annotations

from typing import Any

from xianyu_agent.application.message.send_service import SendAttemptStatus, SendMessageService
from xianyu_agent.infrastructure.message import AuditLogMessageAttemptRecorder, DomainMessageStore
from xianyu_agent.protocol.ws.message_adapter import WsClientMessageProtocol


async def send_worker_text(
    client: Any,
    *,
    account_id: str,
    chat_id: str,
    text: str,
) -> bool:
    """Send one worker-originated message through the canonical application boundary."""
    service = SendMessageService(
        WsClientMessageProtocol(client),
        AuditLogMessageAttemptRecorder(),
        DomainMessageStore(),
    )
    result = await service.send_text(
        account_id=account_id,
        chat_id=chat_id,
        text=text,
    )
    return result.status is SendAttemptStatus.SUCCESS
