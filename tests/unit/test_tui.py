"""Unit tests for the Textual dashboard app (Pilot-driven)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import (
    accounts as domain_accounts,
    cards as domain_cards,
    messages as domain_messages,
)
from xianyu_agent.protocol.events import MessageContentType, MessageReceived
from xianyu_agent.services.guardrails import write_guardrail_event
from xianyu_agent.tui.app import DashboardApp
from xianyu_agent.tui.widgets import AccountsPanel, CardsPanel, MessagesPanel, OrdersPanel


@pytest.fixture
async def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "tui.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-t", nickname="测试", remark="tui")
    await domain_cards.create_card("acc-t", "卡A", "T-1\nT-2", type_="text")
    await domain_messages.upsert_inbound(
        MessageReceived(
            event_id="e-t",
            account_id="acc-t",
            received_at=datetime.now(UTC),
            chat_id="chat-t",
            message_id="m-t",
            sender_id="buyer-t",
            sender_name="买家",
            content_type=MessageContentType.TEXT,
            content="你好,在吗",
        )
    )
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_dashboard_renders_data_and_quits(seeded_db) -> None:
    app = DashboardApp()
    async with app.run_test() as pilot:
        # Let the initial on_mount refresh + intervals settle.
        await pilot.pause()
        await pilot.pause()

        accounts = app.query_one(AccountsPanel)
        assert len(accounts.rows) >= 1
        first_key = next(iter(accounts.rows))
        assert "acc-t" in accounts.get_row(first_key)

        messages = app.query_one(MessagesPanel)
        assert len(messages.rows) >= 1

        cards = app.query_one(CardsPanel)
        assert len(cards.rows) >= 1

        orders = app.query_one(OrdersPanel)
        assert orders is not None

        # Emergency toggle writes an audit row and updates status bar.
        await pilot.press("!")
        await pilot.pause()
        assert app.emergency is True
        assert "紧急" in str(app._status.content)

        # q quits.
        await pilot.press("q")
    assert app.emergency is True


@pytest.mark.asyncio
async def test_dashboard_shows_guardrail_banner(seeded_db) -> None:
    await write_guardrail_event("acc-t", rule="order_amount", detail="金额超限")
    app = DashboardApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "风险" in str(app._risk.content)
        assert "金额超限" in str(app._risk.content)
        await pilot.press("q")
