"""Coverage-gap tests: edge branches across protocol / domain / services."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.cli.main import app as cli_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import (
    Message,
    Order,
    database as db_mod,
)
from xianyu_agent.db.models import ReplyRule
from xianyu_agent.domain import (
    accounts as domain_accounts,
    cards as domain_cards,
    messages as domain_messages,
    orders as domain_orders,
    rules as domain_rules,
)
from xianyu_agent.protocol.events import (
    MessageContentType,
    MessageDirection,
    MessageReceived,
    MessageSent,
    OrderCreated,
    OrderDelivered,
    OrderPaid,
    WsFrame,
)
from xianyu_agent.protocol.parser import (
    _decode_body,
    _parse_order,
    _truncate_raw,
    parse_frame,
)
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.services.account_worker import AccountWorker
from xianyu_agent.services.delivery_service import DeliveryService
from xianyu_agent.services.guardrails import Guardrails, write_guardrail_event
from xianyu_agent.services.heartbeat import purge_old_messages


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "gaps.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


# ============================================================================
# parser edge branches
# ============================================================================


def test_decode_body_non_str() -> None:
    assert _decode_body(123) is None
    assert _decode_body(None) is None
    assert parse_frame(WsFrame(body=None), "acc") == []


def test_decode_body_json_list_then_base64_fail() -> None:
    assert _decode_body('["a", "b"]') is None
    b64 = base64.b64encode(b'["a"]').decode()
    assert _decode_body(b64) is None


def test_truncate_raw_edges() -> None:
    assert _truncate_raw(None) is None
    assert _truncate_raw({"a": 1}) == {"a": 1}
    assert _truncate_raw({"a": "x" * 5000})["_truncated"].startswith('{"a": "xxx')
    # non-serializable raw
    assert _truncate_raw({"a": {1, 2}}) is None


def test_parse_message_no_mid_no_extra() -> None:
    raw = WsFrame(
        body={
            "bizType": "text",
            "1": "hello",
            "2": "buyer",
            "4": "text",
            "6": "not-a-dict",
            "10": "chat-x",
        }
    )
    events = parse_frame(raw, "seller")
    assert len(events) == 1
    assert events[0].message_id is None
    assert events[0].image_url is None


def test_parse_order_delivered_branch() -> None:
    raw = WsFrame(
        body={
            "bizType": "order",
            "5": {"orderId": "O-D", "status": "delivered"},
            "6": {"time": 1700000000000},
        }
    )
    events = parse_frame(raw, "seller")
    assert len(events) == 1
    assert isinstance(events[0], OrderDelivered)


def test_parse_message_unknown_type_falls_back_to_text() -> None:
    raw = WsFrame(
        body={
            "bizType": "text",
            "1": "hello",
            "2": "buyer",
            "4": "weird-type",
            "10": "chat-x",
        }
    )
    events = parse_frame(raw, "seller")
    assert events[0].content_type == MessageContentType.TEXT


def test_parse_message_with_image_url() -> None:
    raw = WsFrame(
        body={
            "bizType": "image",
            "1": "图",
            "2": "buyer",
            "4": "image",
            "5": {"url": "http://img/x.png"},
            "10": "chat-x",
        }
    )
    events = parse_frame(raw, "seller")
    assert events[0].image_url == "http://img/x.png"


def test_parse_order_non_dict_extra() -> None:
    raw = WsFrame(body={"bizType": "order", "5": "not-a-dict"})
    assert parse_frame(raw, "seller") == []


def test_parse_system_no_content_and_non_dict_extra() -> None:
    raw = WsFrame(body={"bizType": "system", "1": "", "5": "x"})
    assert parse_frame(raw, "seller") == []


def test_parse_system_general_notice_with_non_dict_extra() -> None:
    raw = WsFrame(body={"bizType": "system", "1": "系统提醒", "5": "not-dict"})
    events = parse_frame(raw, "seller")
    assert len(events) == 1
    assert events[0].notice_type == "general"


def test_parse_system_via_msg_type_field() -> None:
    raw = WsFrame(body={"1": "异地登录提醒", "4": "system", "5": {"type": "risk"}})
    events = parse_frame(raw, "seller")
    assert len(events) == 1
    assert events[0].notice_type == "risk"


def test_parse_unhandled_biz_type() -> None:
    raw = WsFrame(body={"bizType": "unknown-something"})
    assert parse_frame(raw, "seller") == []


def test_parse_order_unknown_kind_direct() -> None:
    # _parse_order's final fallthrough for an unrecognized kind
    result = _parse_order(
        {"5": {"orderId": "O-X", "buyerId": "b", "amount": 1.0}},
        account_id="seller",
        raw={},
        kind="mystery",
    )
    assert result is None


# ============================================================================
# rules branches
# ============================================================================


@pytest.mark.asyncio
async def test_rules_edge_branches(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    rule = await domain_rules.create_rule(
        "img", "keyword", "卡", "带图回复", reply_image_url="http://x/1.png"
    )
    assert rule.reply_image_url == "http://x/1.png"

    # list_rules for unknown account -> []
    assert await domain_rules.list_rules(account_id="ghost") == []

    # invalid regex never matches
    bad = await domain_rules.create_rule("bad", "regex", "[", "x")
    assert await domain_rules.match_for_account("acc-1", "any") == []

    # match_for_account with unknown account -> []
    assert await domain_rules.match_for_account("ghost", "any") == []

    # record_hit on missing rule -> no-op
    await domain_rules.record_hit(99999)

    # disable then match -> excluded
    await domain_rules.set_rule_enabled(bad.id, False)
    assert bad.id not in [r.id for r in await domain_rules.match_for_account("acc-1", "[")]

    # set enabled / delete on missing rule
    assert await domain_rules.set_rule_enabled(99999, True) is False
    assert await domain_rules.delete_rule(99999) is False

    # rule_matches on a disabled rule directly
    disabled = ReplyRule(enabled=False, type="keyword", pattern="x", reply_text="y")
    assert domain_rules.rule_matches(disabled, "x") is False


# ============================================================================
# cards branches
# ============================================================================


@pytest.mark.asyncio
async def test_cards_edge_branches(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    # card without content -> total 0
    empty = await domain_cards.create_card("acc-1", "空卡", None, type_="text")
    assert empty.total == 0
    assert empty.remaining == 0

    # non-text type counts as single payload
    data_card = await domain_cards.create_card("acc-1", "数据卡", '{"k": "v"}', type_="data")
    assert data_card.total == 1
    assert data_card.remaining == 1

    # restock with whitespace-only content -> None
    assert await domain_cards.restock(data_card.id, "   \n  ") is None

    # list/get for unknown account/card
    assert await domain_cards.list_cards(account_id="ghost") == []
    assert await domain_cards.get_card(99999) is None

    # consume unknown card -> None
    assert await domain_cards.consume_card(99999, None) is None
    # set enabled / delete on missing card
    assert await domain_cards.set_card_enabled(99999, False) is False
    assert await domain_cards.delete_card(99999) is False

    # duplicate codes: second consume finds no unused line -> None
    dup = await domain_cards.create_card("acc-1", "重复卡", "DUP\nDUP", type_="text")
    assert await domain_cards.consume_card(dup.id, None) == "DUP"
    assert await domain_cards.consume_card(dup.id, None) is None


@pytest.mark.asyncio
async def test_consume_card_concurrent_guard(clean_db) -> None:
    """Two concurrent consumes on a 1-code card: exactly one wins."""
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card("acc-1", "单码卡", "ONLY-1", type_="text")

    results = await asyncio.gather(
        domain_cards.consume_card(card.id, None),
        domain_cards.consume_card(card.id, None),
    )
    codes = [r for r in results if r is not None]
    assert codes == ["ONLY-1"]

    got = await domain_cards.get_card(card.id)
    assert got.remaining == 0


# ============================================================================
# pool branches
# ============================================================================


@pytest.mark.asyncio
async def test_pool_unknown_account_ops(clean_db) -> None:
    pool = AccountPool()
    assert await pool.stop("zzz") is False
    assert pool.start("zzz") is False
    assert pool.restart("zzz") is False


@pytest.mark.asyncio
async def test_pool_from_enabled_with_handler(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    calls: list[str] = []

    async def handler(ev) -> None:
        calls.append("x")

    pool = await AccountPool.from_enabled_accounts(on_event=handler)
    assert set(pool.account_ids) == {"acc-1"}
    worker = pool.get("acc-1")
    assert worker._client.on_event is handler
    assert pool.restart("acc-1") is True
    assert pool.start("acc-1") is True


# ============================================================================
# guardrails exception path + delivery gates
# ============================================================================


@pytest.mark.asyncio
async def test_guardrail_write_exception_path(clean_db, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        msg = "db down"
        raise RuntimeError(msg)

    monkeypatch.setattr("xianyu_agent.services.guardrails.get_async_session", boom)
    # must not raise
    await write_guardrail_event("acc-1", rule="x", detail="y")


@pytest.mark.asyncio
async def test_delivery_guardrail_amount_block(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_cards.create_card("acc-1", "卡", "C1", type_="text")
    g = Guardrails(max_order_amount=10.0)
    svc = DeliveryService(sender=lambda a, o, c: True, guardrails=g)
    result = await svc.deliver(
        OrderPaid(
            event_id="e",
            account_id="acc-1",
            received_at=datetime.now(UTC),
            order_id="O-BIG",
            buyer_id="b",
            amount=999.0,
        )
    )
    assert result.delivered is False
    assert "guardrail" in (result.reason or "")


@pytest.mark.asyncio
async def test_delivery_no_sender(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_cards.create_card("acc-1", "卡", "C1", type_="text")
    svc = DeliveryService()  # sender=None
    result = await svc.deliver(
        OrderPaid(
            event_id="e",
            account_id="acc-1",
            received_at=datetime.now(UTC),
            order_id="O-NS",
            buyer_id="b",
            amount=5.0,
        )
    )
    assert result.delivered is False
    assert "未配置发送器" in (result.reason or "")


# ============================================================================
# heartbeat purge
# ============================================================================


@pytest.mark.asyncio
async def test_purge_old_messages(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    old = MessageReceived(
        event_id="old",
        account_id="acc-1",
        received_at=datetime.now(UTC) - timedelta(hours=48),
        chat_id="c",
        sender_id="buyer",
        content_type=MessageContentType.TEXT,
        content="老消息",
    )
    new = MessageReceived(
        event_id="new",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        chat_id="c",
        sender_id="buyer",
        content_type=MessageContentType.TEXT,
        content="新消息",
    )
    await domain_messages.upsert_inbound(old)
    await domain_messages.upsert_inbound(new)

    deleted = await purge_old_messages(older_than_hours=24)
    assert deleted == 1

    async with db_mod.get_async_session() as session:
        rows = list((await session.execute(select(Message))).scalars().all())
    assert len(rows) == 1
    assert rows[0].content == "新消息"

    assert await purge_old_messages(older_than_hours=24) == 0


# ============================================================================
# orders / messages branches
# ============================================================================


@pytest.mark.asyncio
async def test_orders_transitions(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    created = OrderCreated(
        event_id="c",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        order_id="O-T",
        buyer_id="b",
        amount=1.0,
    )
    await domain_orders.upsert_from_event(created)
    paid = OrderPaid(
        event_id="p",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        order_id="O-T",
        buyer_id="b",
        amount=1.0,
        paid_at=datetime.now(UTC),
    )
    await domain_orders.upsert_from_event(paid)
    delivered = OrderDelivered(
        event_id="d",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        order_id="O-T",
        delivered_at=datetime.now(UTC),
    )
    await domain_orders.upsert_from_event(delivered)

    async with db_mod.get_async_session() as session:
        row = (
            await session.execute(select(Order).where(Order.order_id == "O-T").limit(1))
        ).scalar_one()
    assert row.status == "paid"  # ack without content does not flip

    # unknown account -> no order
    ghost_created = OrderCreated(
        event_id="ghost",
        account_id="ghost",
        received_at=datetime.now(UTC),
        order_id="O-GHOST",
        buyer_id="b",
        amount=1.0,
    )
    assert await domain_orders.upsert_from_event(ghost_created) is None
    assert await domain_orders.list_for_account("ghost") == []
    assert await domain_orders.get_by_id(99999) is None


@pytest.mark.asyncio
async def test_messages_outbound_and_query(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    sent = MessageSent(
        event_id="s",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        chat_id="c",
        receiver_id="buyer",
        content_type=MessageContentType.TEXT,
        content="我发出的",
    )
    mid = await domain_messages.record_outbound(sent)
    assert mid is not None

    got = await domain_messages.get_by_id(mid)
    assert got is not None
    assert got.direction == MessageDirection.OUTBOUND.value

    rows = await domain_messages.list_recent(account_id="acc-1", direction="outbound")
    assert len(rows) == 1

    assert (
        await domain_messages.upsert_inbound(
            MessageReceived(
                event_id="x",
                account_id="ghost",
                received_at=datetime.now(UTC),
                chat_id="c",
                sender_id="buyer",
                content_type=MessageContentType.TEXT,
                content="x",
            )
        )
        is None
    )

    # message list for unknown account
    assert await domain_messages.list_recent(account_id="ghost") == []


@pytest.mark.asyncio
async def test_worker_send_text_offline_returns_false(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    worker = AccountWorker("acc-1")
    worker.start()
    try:
        ok = await worker.send_text("在吗")
    finally:
        await worker.stop()
    assert ok is False  # no live socket offline


def test_cli_message_send_offline_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "cli.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(db_mod.init_db())
    asyncio.run(domain_accounts.create_account("acc-1", enabled=True))

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["message", "send", "--account", "acc-1", "--chat-id", "c1", "--text", "在吗"],
    )
    assert result.exit_code == 1
    assert "未连接" in result.output

    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()
