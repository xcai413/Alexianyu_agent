"""BargainGate and ReplyEngine integration: floor enforcement and round accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, database as db_mod
from xianyu_agent.domain import (
    accounts as domain_accounts,
    items as domain_items,
    messages as domain_messages,
    rules as domain_rules,
)
from xianyu_agent.domain.ai import AIRequest, AIResponse
from xianyu_agent.protocol.events import MessageContentType, MessageReceived
from xianyu_agent.protocol.items_client import RemoteItem
from xianyu_agent.services.bargain_gate import BargainGate
from xianyu_agent.services.reply_engine import ReplyEngine


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "bargain.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _event(
    content: str = "多少钱",
    *,
    item_id: str | None = "I-1",
    message_id: str = "m-1",
    chat_id: str = "chat-1",
) -> MessageReceived:
    return MessageReceived(
        event_id=f"e-{message_id}",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        chat_id=chat_id,
        message_id=message_id,
        sender_id="buyer-1",
        sender_name="买家",
        item_id=item_id,
        content_type=MessageContentType.TEXT,
        content=content,
    )


async def _seed_item(item_id: str, price: str) -> None:
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id=item_id,
                title="测试商品",
                price=price,
                status=None,
                detail_url=None,
                main_image_url=None,
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
        ],
    )


class FakeProvider:
    name = "fake"
    provider_type = "fake"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def generate(self, request: AIRequest) -> AIResponse:
        _ = request
        return AIResponse(text=self.reply, provider=self.provider_type)


async def _reply_audits() -> list[AuditLog]:
    async with db_mod.get_async_session() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "message.reply_decision")
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_gate_allows_a_floor_offer_and_consumes_one_round(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    await domain_messages.upsert_inbound(_event())
    gate = BargainGate(minimum_price=Decimal("40"))

    decision = await gate.check(
        account_id="acc-1", chat_id="chat-1", item_id="I-1", reply_text="给您45元包邮"
    )

    assert decision.allowed is True
    assert decision.offer_price == Decimal("45.00")
    assert await domain_messages.get_bargain_round(account_id="acc-1", chat_id="chat-1") == 1


@pytest.mark.asyncio
async def test_gate_blocks_a_below_floor_offer_without_consuming_a_round(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    await domain_messages.upsert_inbound(_event())
    gate = BargainGate(minimum_price=Decimal("40"))

    decision = await gate.check(
        account_id="acc-1", chat_id="chat-1", item_id="I-1", reply_text="给您38元"
    )

    assert decision.allowed is False
    assert decision.reason == "议价低于底价"
    assert await domain_messages.get_bargain_round(account_id="acc-1", chat_id="chat-1") == 0


@pytest.mark.asyncio
async def test_gate_blocks_further_concessions_once_rounds_run_out(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    await domain_messages.upsert_inbound(_event())
    gate = BargainGate(minimum_price=Decimal("40"), max_rounds=1)

    first = await gate.check(
        account_id="acc-1", chat_id="chat-1", item_id="I-1", reply_text="给您45元"
    )
    second = await gate.check(
        account_id="acc-1", chat_id="chat-1", item_id="I-1", reply_text="给您42元"
    )

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "议价轮次已用尽"
    assert await domain_messages.get_bargain_round(account_id="acc-1", chat_id="chat-1") == 1


@pytest.mark.asyncio
async def test_gate_derives_the_floor_from_the_configured_discount_ratio(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    await domain_messages.upsert_inbound(_event())
    gate = BargainGate(max_discount_ratio=Decimal("0.1"))

    decision = await gate.check(
        account_id="acc-1", chat_id="chat-1", item_id="I-1", reply_text="给您打8折"
    )

    assert decision.allowed is False
    assert decision.reason == "议价低于底价"
    assert decision.offer_price == Decimal("40.00")
    assert decision.floor_price == Decimal("45.00")


@pytest.mark.asyncio
async def test_engine_suppresses_an_ai_reply_below_the_floor(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    message_id = await domain_messages.upsert_inbound(_event())
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(
        sender=sender,
        provider=FakeProvider("最低给您35元,亲"),
        mode="AI_ONLY",
        bargain_gate=BargainGate(minimum_price=Decimal("40")),
    )
    await engine.handle(_event(), message_id=message_id)

    assert sent == []
    audits = await _reply_audits()
    assert [row.params.get("reason") for row in audits] == ["议价低于底价"]
    assert audits[0].result == "suppressed"
    assert await domain_messages.get_bargain_round(account_id="acc-1", chat_id="chat-1") == 0


@pytest.mark.parametrize("reply", ["商品0元卖给您", "零元给您", "给您打0折", "给您零折"])
@pytest.mark.parametrize("mode", ["AI_ONLY", "RULE_THEN_AI"])
async def test_default_engine_gate_suppresses_zero_ai_offers(
    clean_db, monkeypatch: pytest.MonkeyPatch, reply: str, mode: str
) -> None:
    monkeypatch.setenv("XIANYU_BARGAIN_MINIMUM_PRICE", "40")
    reset_settings_cache()
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    event = _event()
    message_id = await domain_messages.upsert_inbound(event)
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    # 不注入BargainGate,验证默认引擎确实从本地配置装配价格闸。
    engine = ReplyEngine(sender=sender, provider=FakeProvider(reply), mode=mode)
    await engine.handle(event, message_id=message_id)

    assert sent == []
    audits = await _reply_audits()
    assert [row.params.get("reason") for row in audits] == ["议价低于底价"]
    assert [row.result for row in audits] == ["suppressed"]
    assert reply not in str(audits[0].params)
    assert await domain_messages.get_bargain_round(account_id="acc-1", chat_id="chat-1") == 0


@pytest.mark.asyncio
async def test_engine_sends_an_allowed_ai_offer_and_records_the_round(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    message_id = await domain_messages.upsert_inbound(_event())
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(
        sender=sender,
        provider=FakeProvider("给您45元包邮哦"),
        mode="AI_ONLY",
        bargain_gate=BargainGate(minimum_price=Decimal("40")),
    )
    await engine.handle(_event(), message_id=message_id)

    assert sent == ["给您45元包邮哦"]
    audits = await _reply_audits()
    assert [row.result for row in audits] == ["sent"]
    assert await domain_messages.get_bargain_round(account_id="acc-1", chat_id="chat-1") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["特价35元", "零元"])
async def test_engine_leaves_operator_rule_replies_out_of_the_bargain_gate(
    clean_db, reply: str
) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await _seed_item("I-1", "50")
    await domain_rules.create_rule("k", "keyword", "多少钱", reply)
    message_id = await domain_messages.upsert_inbound(_event())
    sent: list[str] = []

    async def sender(account_id: str, chat_id: str, text: str) -> bool:
        sent.append(text)
        return True

    engine = ReplyEngine(
        sender=sender,
        mode="RULE_ONLY",
        bargain_gate=BargainGate(minimum_price=Decimal("40")),
    )
    await engine.handle(_event(), message_id=message_id)

    assert sent == [reply]
