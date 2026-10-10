"""`order show` 不能把 Phase 6 已发货的订单显示成"未发货"。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.infrastructure.database.models import Account as AccountRow, Order as OrderRow

runner = CliRunner()


@pytest.fixture
async def order_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "order-show.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_order(*, status: str, delivered: bool) -> int:
    async with get_async_session() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == "acc-1").limit(1)
            )
        ).scalar_one()
        row = OrderRow(
            account_id=account.id,
            order_id="O-1",
            item_id="I-1",
            item_title="商品",
            amount=9.9,
            quantity=1,
            status=status,
            placed_at=datetime.now(UTC),
            paid_at=datetime.now(UTC),
            delivered_at=datetime.now(UTC) if delivered else None,
        )
        session.add(row)
        await session.flush()
        return int(row.id)


def test_delivered_order_without_stored_content_is_not_reported_as_undelivered(
    order_db: None,
) -> None:
    order_pk = asyncio.run(_seed_order(status="delivered", delivered=True))

    result = runner.invoke(app, ["order", "show", str(order_pk)])

    assert result.exit_code == 0, result.output
    assert "已发货" in result.output
    # Phase 6 不把卡密写进订单行,所以只能显示占位,不能显示成"未发货"。
    assert "(未发货)" not in result.output
    assert "内容见发货任务" in result.output


def test_pending_order_is_still_reported_as_not_delivered(order_db: None) -> None:
    order_pk = asyncio.run(_seed_order(status="paid", delivered=False))

    result = runner.invoke(app, ["order", "show", str(order_pk)])

    assert result.exit_code == 0, result.output
    assert "尚未发货" in result.output
