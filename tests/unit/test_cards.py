"""Unit tests for card inventory domain (CRUD + atomic consumption)."""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, cards as domain_cards


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "cards.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_card_create_counts_lines(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card(
        "acc-1",
        "文本卡",
        "CODE-1\nCODE-2\nCODE-3\n",
        type_="text",
        unit_price=9.9,
        description="测试卡",
    )
    assert card.total == 3
    assert card.remaining == 3
    assert card.unit_price == 9.9

    with pytest.raises(ValueError, match="type_"):
        await domain_cards.create_card("acc-1", "bad", "x", type_="nope")
    with pytest.raises(ValueError, match="不存在"):
        await domain_cards.create_card("ghost", "bad", "x")


@pytest.mark.asyncio
async def test_consume_atomic_and_out_of_stock(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card("acc-1", "文本卡", "C1\nC2", type_="text")

    # consume one by one, in order, no duplicates
    assert await domain_cards.consume_card(card.id, None) == "C1"
    assert await domain_cards.consume_card(card.id, None) == "C2"
    assert await domain_cards.consume_card(card.id, None) is None  # out of stock

    got = await domain_cards.get_card(card.id)
    assert got.remaining == 0

    consumptions = await domain_cards.recent_consumptions()
    assert len(consumptions) == 2
    assert {c.content for c in consumptions} == {"C1", "C2"}


@pytest.mark.asyncio
async def test_consume_disabled_card(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card("acc-1", "卡", "X1", type_="text")
    await domain_cards.set_card_enabled(card.id, False)
    assert await domain_cards.consume_card(card.id, None) is None


@pytest.mark.asyncio
async def test_restock_and_list(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)
    card = await domain_cards.create_card("acc-1", "卡", "A1", type_="text")
    assert await domain_cards.consume_card(card.id, None) == "A1"

    updated = await domain_cards.restock(card.id, "B1\nB2")
    assert updated is not None
    assert updated.total == 3
    assert updated.remaining == 2

    assert await domain_cards.restock(9999, "x") is None

    rows = await domain_cards.list_cards(account_id="acc-1")
    assert len(rows) == 1
    assert len(await domain_cards.list_cards(account_id="acc-2")) == 0

    assert await domain_cards.set_card_enabled(card.id, False) is True
    assert len(await domain_cards.list_cards(account_id="acc-1", only_enabled=True)) == 0

    assert await domain_cards.delete_card(card.id) is True
    assert await domain_cards.get_card(card.id) is None
