"""Unit tests for ReplyEngine: match -> send -> log, failure paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import ReplyLog, database as db_mod
from xianyu_agent.domain import (
    accounts as domain_accounts,
    messages as domain_messages,
    rules as domain_rules,
)
from xianyu_agent.protocol.events import MessageContentType, MessageReceived
from xianyu_agent.services.ai_provider import UnconfiguredError
from xianyu_agent.services.reply_engine import ReplyEngine


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "engine.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _event(
    account_id: str = "acc-1", content: str = "在吗", chat_id: str = "chat-1"
) -> MessageReceived:
    return MessageReceived(
        event_id="e-1",
        account_id=account_id,
        received_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        chat_id=chat_id,
        message_id="m-1",
        sender_id="buyer-1",
        sender_name="买家",
        content_type=MessageContentType.TEXT,
        content=content,
    )


@pytest.mark.asyncio
async def test_engine_sends_and_logs(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    rule = await domain_rules.create_rule("k", "keyword", "在吗", "在的,亲")
    sent: list[str] = []
    message_id = await domain_messages.upsert_inbound(_event())

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append((account_id, chat_id, text))
        return True

    engine = ReplyEngine(sender=sender)
    await engine.handle(_event(), message_id=message_id)

    assert sent == [("acc-1", "chat-1", "在的,亲")]
    async with db_mod.get_async_session() as session:
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
    assert len(logs) == 1
    assert logs[0].rule_id == rule.id
    assert logs[0].success is True
    assert logs[0].message_id == message_id

    # hit count incremented
    got = await domain_rules.get_rule(rule.id)
    assert got.hit_count == 1


@pytest.mark.asyncio
async def test_engine_no_rule_no_send(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(sender=sender)
    await engine.handle(_event(content="没有任何规则命中"))
    assert sent == []


@pytest.mark.asyncio
async def test_engine_send_failure_logged(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("k", "keyword", "在吗", "在的,亲")
    message_id = await domain_messages.upsert_inbound(_event())

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        return False

    engine = ReplyEngine(sender=sender)
    await engine.handle(_event(), message_id=message_id)

    async with db_mod.get_async_session() as session:
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
    assert len(logs) == 1
    assert logs[0].success is False


@pytest.mark.asyncio
async def test_engine_sender_exception_logged(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("k", "keyword", "在吗", "在的,亲")
    message_id = await domain_messages.upsert_inbound(_event())

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        msg = "boom"
        raise RuntimeError(msg)

    engine = ReplyEngine(sender=sender)
    await engine.handle(_event(), message_id=message_id)

    async with db_mod.get_async_session() as session:
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
    assert len(logs) == 1
    assert logs[0].success is False
    assert logs[0].error is not None
    assert "boom" in logs[0].error


@pytest.mark.asyncio
async def test_engine_auto_reply_disabled(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("k", "keyword", "在吗", "在的,亲")
    sent: list[str] = []
    message_id = await domain_messages.upsert_inbound(_event())

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(sender=sender, auto_reply=False)
    await engine.handle(_event(), message_id=message_id)
    assert sent == []


class FakeProvider:
    name = "fake"

    def __init__(self, reply: str = "AI 回复", *, raise_unconfigured: bool = False) -> None:
        self.reply = reply
        self.raise_unconfigured = raise_unconfigured
        self.calls: list[tuple[str, str]] = []

    async def generate_reply(
        self, *, account_id: str, buyer_message: str, chat_context=None
    ) -> str:
        self.calls.append((account_id, buyer_message))
        if self.raise_unconfigured:
            msg = "未配置"
            raise UnconfiguredError(msg)
        return self.reply


@pytest.mark.asyncio
async def test_mode_rule_ignores_provider(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("k", "keyword", "在吗", "规则回复")
    provider = FakeProvider()
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(sender=sender, provider=provider, mode="rule")
    await engine.handle(_event(), message_id=None)
    assert sent == ["规则回复"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_mode_rule_then_ai_rule_wins(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("k", "keyword", "在吗", "规则回复")
    provider = FakeProvider(reply="AI 回复")
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(sender=sender, provider=provider, mode="rule_then_ai")
    await engine.handle(_event(), message_id=None)
    assert sent == ["规则回复"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_mode_rule_then_ai_fallback_to_ai(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    provider = FakeProvider(reply="AI 回复")
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(sender=sender, provider=provider, mode="rule_then_ai")
    await engine.handle(_event(content="没有任何规则命中"), message_id=None)
    assert sent == ["AI 回复"]
    assert provider.calls == [("acc-1", "没有任何规则命中")]

    async with db_mod.get_async_session() as session:
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
    assert logs[0].source == "ai"
    assert logs[0].rule_id is None


@pytest.mark.asyncio
async def test_mode_ai_prefers_provider(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("k", "keyword", "在吗", "规则回复")
    provider = FakeProvider(reply="AI 回复")
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(sender=sender, provider=provider, mode="ai")
    await engine.handle(_event(), message_id=None)
    assert sent == ["AI 回复"]


@pytest.mark.asyncio
async def test_mode_ai_unconfigured_falls_back_silently(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    provider = FakeProvider(raise_unconfigured=True)
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(sender=sender, provider=provider, mode="ai")
    await engine.handle(_event(), message_id=None)
    assert sent == []
