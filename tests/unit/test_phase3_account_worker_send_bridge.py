from __future__ import annotations

import pytest

from xianyu_agent.runtime.account_worker import AccountWorker


class _LegacyReplyClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return True


@pytest.mark.asyncio
async def test_worker_auto_reply_stays_on_legacy_send_path_until_manual_calibration() -> None:
    client = _LegacyReplyClient()
    worker = object.__new__(AccountWorker)
    worker._client = client

    ok = await worker._send_reply("acc-1", "chat-1", "hello")

    assert ok is True
    assert client.sent == ["hello"]
