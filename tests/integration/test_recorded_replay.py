"""Integration test: replay recorded frames through the full worker pipeline.

Uses tests/fixtures/sample_frames.jsonl (5 recorded-style frames) and drives
AccountWorker directly — no live WS needed. Verifies message persistence,
rule auto-reply attempt, card consumption, and honest failure recording.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import (
    CardConsumption,
    Message,
    Order,
    ReplyLog,
    database as db_mod,
    get_async_session,
)
from xianyu_agent.domain import (
    accounts as domain_accounts,
    cards as domain_cards,
    rules as domain_rules,
)
from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.services.account_worker import AccountWorker

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_frames.jsonl"


@pytest.mark.asyncio
async def test_recorded_replay_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "replay.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    await domain_accounts.create_account("demo", enabled=True)
    await domain_rules.create_rule(
        "在吗规则", "keyword", "还在吗", "在的,亲", account_id="demo", priority=10
    )
    await domain_cards.create_card("demo", "9.9卡", "CARD-A001\nCARD-A002", type_="text")

    frames = [
        WsFrame.model_validate(json.loads(line))
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(frames) == 5

    worker = AccountWorker("demo")
    worker.start()
    for f in frames:
        worker.inject_frame(f)
    await asyncio.sleep(2.0)
    await worker.stop()

    async with get_async_session() as session:
        msgs = list((await session.execute(select(Message))).scalars().all())
        orders = list((await session.execute(select(Order))).scalars().all())
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
        cons = list((await session.execute(select(CardConsumption))).scalars().all())

    # 2 inbound messages from the fixture persisted
    assert len(msgs) == 2
    # keyword rule matched "还在吗" -> reply attempted (offline send fails honestly)
    assert len(logs) == 1
    assert logs[0].rule_id is not None
    assert logs[0].success is False  # no live socket
    # paid order -> card consumed -> send failed -> stays paid with reason
    assert len(orders) == 1
    assert orders[0].order_id == "O-9001"
    assert orders[0].status == "paid"
    assert orders[0].delivery_fail_reason is not None
    assert len(cons) == 1
    assert cons[0].content == "CARD-A001"
    # delivered ack did NOT mask the failure
    assert orders[0].delivery_content is None

    await db_mod.async_engine.dispose()
    reset_settings_cache()
