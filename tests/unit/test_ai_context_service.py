from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from xianyu_agent.application.ai import context_service as context_module
from xianyu_agent.application.ai.context_service import ContextService


@pytest.mark.asyncio
async def test_context_is_bounded_chronological_and_excludes_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def list_recent(**kwargs: object) -> list[SimpleNamespace]:
        captured.update(kwargs)
        return [
            SimpleNamespace(id=4, content="当前消息"),
            SimpleNamespace(id=3, content="第三条"),
            SimpleNamespace(id=2, content="第二条"),
            SimpleNamespace(id=1, content="第一条"),
        ]

    monkeypatch.setattr(context_module.domain_messages, "list_recent", list_recent)
    now = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)

    async def get_item_by_external_id(_account_id: str, _item_id: str) -> None:
        return None

    monkeypatch.setattr(
        context_module.domain_items, "get_item_by_external_id", get_item_by_external_id
    )
    result = await ContextService(max_messages=2, max_age_seconds=3600).build(
        account_id="acc-1", chat_id="chat-1", current_message_id=4, item_id=None, now=now
    )

    assert result.messages == ("第二条", "第三条")
    assert result.item_facts is None
    assert captured["limit"] == 3
    assert captured["since"] == now - timedelta(hours=1)


@pytest.mark.asyncio
async def test_zero_context_limit_avoids_message_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_recent(**_kwargs: object) -> list[SimpleNamespace]:
        raise AssertionError("zero context limit must not query messages")

    monkeypatch.setattr(context_module.domain_messages, "list_recent", list_recent)

    async def get_item_by_external_id(_account_id: str, _item_id: str) -> None:
        return None

    monkeypatch.setattr(
        context_module.domain_items, "get_item_by_external_id", get_item_by_external_id
    )
    result = await ContextService(max_messages=0, max_age_seconds=60).build(
        account_id="acc-1", chat_id="chat-1", current_message_id=None, item_id=None
    )

    assert result.messages == ()


@pytest.mark.asyncio
async def test_context_adds_only_safe_local_item_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_recent(**_kwargs: object) -> list[SimpleNamespace]:
        return []

    async def get_item_by_external_id(account_id: str, item_id: str) -> SimpleNamespace:
        assert (account_id, item_id) == ("acc-1", "item-1")
        return SimpleNamespace(title="测试商品", price="9.90", is_on_sale=True)

    monkeypatch.setattr(context_module.domain_messages, "list_recent", list_recent)
    monkeypatch.setattr(
        context_module.domain_items, "get_item_by_external_id", get_item_by_external_id
    )

    result = await ContextService(max_messages=2, max_age_seconds=60).build(
        account_id="acc-1", chat_id="chat-1", current_message_id=None, item_id="item-1"
    )

    assert result.item_facts == (
        "商品事实 (本会话唯一的商品事实来源):\n- 标题: 测试商品\n- 价格: 9.90\n- 状态: 在售"
    )
