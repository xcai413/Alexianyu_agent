"""Compatibility facade for the canonical runtime account worker."""

from __future__ import annotations

import asyncio

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
        wait_ready_s: float = 10.0,
    ) -> SendMessageResult:
        await self._wait_message_protocol_ready(wait_ready_s)
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

    async def _wait_message_protocol_ready(self, timeout_s: float) -> None:
        """Wait for readiness only; never perform or retry an external send here."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        while loop.time() < deadline:
            try:
                self._client._require_protocol_context()
            except ConnectionError:
                pass
            else:
                account_user_id = getattr(self._client, "_account_user_id", None)
                if isinstance(account_user_id, str) and account_user_id.strip():
                    return
            await asyncio.sleep(0.05)


__all__ = ["AccountWorker"]
