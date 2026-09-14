from __future__ import annotations

from typing import Any

import pytest

from xianyu_agent.runtime.account_worker import AccountWorker


class _LegacyReplyClient:
    async def send_text(self, _text: str) -> bool:
        raise AssertionError("auto-reply must not bypass the canonical send bridge")


@pytest.mark.asyncio
async def test_worker_auto_reply_uses_canonical_send_bridge(monkeypatch) -> None:
    client = _LegacyReplyClient()
    worker = object.__new__(AccountWorker)
    worker._client = client

    captured: dict[str, Any] = {}

    async def fake_send_worker_text(
        actual_client: Any,
        *,
        account_id: str,
        chat_id: str,
        text: str,
    ) -> bool:
        captured.update(
            client=actual_client,
            account_id=account_id,
            chat_id=chat_id,
            text=text,
        )
        return True

    monkeypatch.setattr(
        "xianyu_agent.runtime.account_worker.send_worker_text",
        fake_send_worker_text,
    )

    ok = await worker._send_reply("acc-1", "chat-1", "hello")

    assert ok is True
    assert captured == {
        "client": client,
        "account_id": "acc-1",
        "chat_id": "chat-1",
        "text": "hello",
    }


@pytest.mark.asyncio
async def test_worker_auto_reply_propagates_canonical_send_failure(monkeypatch) -> None:
    worker = object.__new__(AccountWorker)
    worker._client = _LegacyReplyClient()

    async def fake_send_worker_text(
        _client: Any,
        *,
        account_id: str,
        chat_id: str,
        text: str,
    ) -> bool:
        assert (account_id, chat_id, text) == ("acc-1", "chat-1", "hello")
        return False

    monkeypatch.setattr(
        "xianyu_agent.runtime.account_worker.send_worker_text",
        fake_send_worker_text,
    )

    assert await worker._send_reply("acc-1", "chat-1", "hello") is False
