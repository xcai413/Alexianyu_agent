"""Compatibility facade for the canonical runtime account worker."""

from __future__ import annotations

from xianyu_agent.application.message.send_service import SendMessageResult, SendMessageService
from xianyu_agent.infrastructure.message import AuditLogMessageAttemptRecorder
from xianyu_agent.protocol.ws.message_adapter import WsClientMessageProtocol
from xianyu_agent.runtime.account_worker import AccountWorker as RuntimeAccountWorker


class AccountWorker(RuntimeAccountWorker):
    """Runtime worker plus the Phase 3 canonical business-message capability."""

    async def send_message(
        self,
        *,
        chat_id: str,
        receiver_id: str,
        text: str,
    ) -> SendMessageResult:
        service = SendMessageService(
            WsClientMessageProtocol(self._client),
            AuditLogMessageAttemptRecorder(),
        )
        return await service.send_text(
            account_id=self.account_id,
            chat_id=chat_id,
            receiver_id=receiver_id,
            text=text,
        )


__all__ = ["AccountWorker"]
