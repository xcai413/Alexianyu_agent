"""Unit tests for ReplyEngine: match -> send -> log, failure paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, ReplyLog, database as db_mod
from xianyu_agent.domain import (
    accounts as domain_accounts,
    messages as domain_messages,
    rules as domain_rules,
)
from xianyu_agent.domain.ai import AIRequest, AIResponse, UnconfiguredError
from xianyu_agent.protocol.events import MessageContentType, MessageReceived
from xianyu_agent.services.message_filter import MessageFilter
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
    account_id: str = "acc-1",
    content: str = "在吗",
    chat_id: str = "chat-1",
    message_id: str = "m-1",
) -> MessageReceived:
    return MessageReceived(
        event_id=f"e-{message_id}",
        account_id=account_id,
        received_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        chat_id=chat_id,
        message_id=message_id,
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

    async with db_mod.get_async_session() as session:
        decision = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "message.reply_decision")
            )
        ).scalar_one()
    assert decision.result == "sent"
    assert decision.params == {
        "outcome": "sent",
        "message_id": message_id,
        "source": "rule",
        "rule_id": rule.id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", [None, "not-mirrored"])
@pytest.mark.parametrize("local_scope", ["account", "global", "none"])
async def test_engine_does_not_send_other_account_template(
    clean_db, item_id: str | None, local_scope: str
) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)
    foreign = await domain_rules.create_rule(
        "其他账号", "keyword", "在吗", "其他账号模板", account_id="acc-2", priority=1
    )
    if local_scope != "none":
        await domain_rules.create_rule(
            "允许的模板",
            "keyword",
            "在吗",
            "允许的回复",
            account_id="acc-1" if local_scope == "account" else None,
            priority=100,
        )
    event = _event().model_copy(update={"item_id": item_id})
    message_id = await domain_messages.upsert_inbound(event)
    sent: list[tuple[str, str, str]] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append((account_id, chat_id, text))
        return True

    await ReplyEngine(sender=sender, mode="RULE_ONLY").handle(event, message_id=message_id)
    expected = [] if local_scope == "none" else [("acc-1", "chat-1", "允许的回复")]
    assert sent == expected
    async with db_mod.get_async_session() as session:
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
    assert all(log.rule_id != foreign.id for log in logs)


@pytest.mark.asyncio
async def test_engine_reply_once_suppresses_second_message_in_same_conversation(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("一次回复", "keyword", "在吗", "只回复一次", reply_once=True)
    sent: list[tuple[str, str, str]] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append((account_id, chat_id, text))
        return True

    engine = ReplyEngine(sender=sender)
    first = _event(message_id="once-1")
    second = _event(message_id="once-2")
    first_id = await domain_messages.upsert_inbound(first)
    await engine.handle(first, message_id=first_id)
    second_id = await domain_messages.upsert_inbound(second)
    await engine.handle(second, message_id=second_id)

    assert sent == [("acc-1", "chat-1", "只回复一次")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setter", "state"),
    [
        (domain_messages.set_bot_state, "paused"),
        (domain_messages.set_takeover_state, "human"),
    ],
)
async def test_engine_suppresses_reply_for_paused_or_human_conversation(
    clean_db, setter, state
) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("问候", "keyword", "在吗", "在的")
    event = _event()
    message_id = await domain_messages.upsert_inbound(event)
    await setter(account_id="acc-1", chat_id=event.chat_id, state=state)
    sent: list[tuple[str, str, str]] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append((account_id, chat_id, text))
        return True

    await ReplyEngine(sender=sender).handle(event, message_id=message_id)

    assert sent == []


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


@pytest.mark.asyncio
async def test_engine_filter_blocks_send_and_audits_without_content(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("k", "keyword", "验收", "不应发送")
    event = _event(content="验收-拦截")
    message_id = await domain_messages.upsert_inbound(event)
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    await ReplyEngine(
        sender=sender, message_filter=MessageFilter(blocked_patterns=["拦截"])
    ).handle(event, message_id=message_id)

    assert sent == []
    async with db_mod.get_async_session() as session:
        decision = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "message.reply_decision")
            )
        ).scalar_one()
    assert decision.result == "suppressed"
    assert decision.params == {
        "outcome": "suppressed",
        "message_id": message_id,
        "reason": "匹配消息过滤规则 #1",
    }


class FakeProvider:
    name = "fake"
    provider_type = "fake"

    def __init__(self, reply: str = "AI 回复", *, raise_unconfigured: bool = False) -> None:
        self.reply = reply
        self.raise_unconfigured = raise_unconfigured
        self.calls: list[tuple[str, str]] = []

    async def generate(self, request: AIRequest) -> AIResponse:
        self.calls.append((request.account_id, request.buyer_message))
        if self.raise_unconfigured:
            msg = "未配置"
            raise UnconfiguredError(msg)
        return AIResponse(text=self.reply, provider=self.provider_type)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "content", "expected"),
    [
        ("RULE_ONLY", "命中", "关键词"),
        ("RULE_ONLY", "其他消息", "默认"),
        ("RULE_THEN_AI", "命中", "关键词"),
        ("RULE_THEN_AI", "其他消息", "AI 回复"),
        ("RULE_THEN_DEFAULT", "其他消息", "默认"),
        ("RULE_DEFAULT_THEN_AI", "其他消息", "默认"),
        ("AI_ONLY", "命中", "AI 回复"),
    ],
)
async def test_m9_reply_pipelines_choose_explicit_order(
    clean_db, mode: str, content: str, expected: str
) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_rules.create_rule("关键词", "keyword", "命中", "关键词")
    await domain_rules.create_rule("默认", "default", "", "默认")
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    await ReplyEngine(sender=sender, provider=FakeProvider(), mode=mode).handle(
        _event(content=content, message_id=f"pipeline-{mode}")
    )

    assert sent == [expected]


@pytest.mark.asyncio
async def test_ai_reply_blocked_by_safety_policy_is_audited_without_content(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    event = _event(content="多少钱")
    message_id = await domain_messages.upsert_inbound(event)
    provider = FakeProvider(reply="加我微信 abc12345,给你优惠")
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    await ReplyEngine(sender=sender, provider=provider, mode="ai").handle(
        event, message_id=message_id
    )

    assert sent == []
    async with db_mod.get_async_session() as session:
        decision = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "message.reply_decision")
            )
        ).scalar_one()
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
    assert decision.result == "suppressed"
    assert decision.params == {
        "outcome": "suppressed",
        "message_id": message_id,
        "reason": "命中输出安全规则: 站外联系方式",
        "source": "ai",
    }
    assert logs == []
