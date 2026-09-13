from __future__ import annotations

from typing import Any

import pytest

from xianyu_agent.runtime import account_worker as account_worker_module
from xianyu_agent.runtime.account_worker import AccountWorker


@pytest.mark.asyncio
async def test_worker_reply_routes_account_chat_and_text_through_canonical_send_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    client = object()

    async def fake_send_worker_text(
        actual_client: Any,
        *,
        account_id: str,
        chat_id: str,
        text: str,
    ) -> bool:
        calls.append(
            {
                "client": actual_client,
                "account_id": account_id,
                "chat_id": chat_id,
                "text": text,
            }
        )
        return True

    monkeypatch.setattr(account_worker_module, "send_worker_text", fake_send_worker_text)
    worker = object.__new__(AccountWorker)
    worker._client = client

    ok = await worker._send_reply("acc-1", "chat-1", "hello")

    assert ok is True
    assert calls == [
        {
            "client": client,
            "account_id": "acc-1",
            "chat_id": "chat-1",
            "text": "hello",
        }
    ]
