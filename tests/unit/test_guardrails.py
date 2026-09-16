"""Unit tests for guardrails: rate / quiet hours / amount / circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, messages as domain_messages
from xianyu_agent.protocol.events import MessageContentType, MessageReceived
from xianyu_agent.services.guardrails import (
    Guardrails,
    recent_guardrail_events,
    write_guardrail_event,
)


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "guard.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-g", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _event(content: str, i: int = 0) -> MessageReceived:
    return MessageReceived(
        event_id=f"e-{i}",
        account_id="acc-g",
        received_at=datetime.now(UTC),
        chat_id="chat-g",
        message_id=f"m-{i}",
        sender_id="buyer",
        content_type=MessageContentType.TEXT,
        content=content,
    )


def test_quiet_hours_window() -> None:
    g = Guardrails(quiet_hours="23:00-08:00")
    assert g._in_quiet_hours(datetime(2026, 1, 1, 23, 30, tzinfo=UTC)) is True
    assert g._in_quiet_hours(datetime(2026, 1, 1, 2, 0, tzinfo=UTC)) is True
    assert g._in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=UTC)) is False
    # non-crossing window
    g2 = Guardrails(quiet_hours="01:00-03:00")
    assert g2._in_quiet_hours(datetime(2026, 1, 1, 2, 0, tzinfo=UTC)) is True
    assert g2._in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=UTC)) is False
    # malformed -> never quiet
    g3 = Guardrails(quiet_hours="garbage")
    assert g3._in_quiet_hours(datetime(2026, 1, 1, 2, 0, tzinfo=UTC)) is False


@pytest.mark.asyncio
async def test_message_rate_gate(clean_db) -> None:
    g = Guardrails(max_msg_per_hour=2, quiet_hours="")
    # one message -> allowed
    decision = await g.check_message("acc-g", "在吗")
    assert decision.allowed is True

    await domain_messages.upsert_inbound(_event("在吗", 1))
    await domain_messages.upsert_inbound(_event("多少钱", 2))
    decision = await g.check_message("acc-g", "在吗")
    assert decision.allowed is False
    assert "频率" in (decision.reason or "")


@pytest.mark.asyncio
async def test_quiet_hours_blocks_message(clean_db) -> None:
    g = Guardrails(quiet_hours="23:00-08:00")
    now = datetime.now(UTC)
    if g._in_quiet_hours(now):
        decision = await g.check_message("acc-g", "在吗")
        assert decision.allowed is False
        assert "静默" in (decision.reason or "")
    else:
        decision = await g.check_message("acc-g", "在吗")
        assert decision.allowed is True


@pytest.mark.asyncio
async def test_order_amount_gate(clean_db) -> None:
    g = Guardrails(max_order_amount=100.0)
    assert (await g.check_delivery("acc-g", 50.0)).allowed is True
    blocked = await g.check_delivery("acc-g", 999.0)
    assert blocked.allowed is False
    assert "金额" in (blocked.reason or "")


@pytest.mark.asyncio
async def test_circuit_breaker(clean_db) -> None:
    g = Guardrails(fail_threshold=3)
    assert await g.record_send_result("acc-g", True) is None
    assert await g.record_send_result("acc-g", False) is None
    assert await g.record_send_result("acc-g", False) is None
    decision = await g.record_send_result("acc-g", False)
    assert decision is not None
    assert decision.allowed is False
    assert "熔断" in (decision.reason or "")
    # success resets
    assert await g.record_send_result("acc-g", True) is None
    assert await g.record_send_result("acc-g", False) is None
    assert await g.record_send_result("acc-g", False) is None
    decision2 = await g.record_send_result("acc-g", False)  # threshold 3 again
    assert decision2 is not None
    assert decision2.allowed is False


@pytest.mark.asyncio
async def test_guardrail_event_visible(clean_db) -> None:
    await write_guardrail_event("acc-g", rule="order_amount", detail="金额超限")
    events = await recent_guardrail_events()
    assert len(events) == 1
    assert events[0]["account_id"] == "acc-g"
    assert events[0]["rule"] == "order_amount"
    assert events[0]["detail"] == "金额超限"
