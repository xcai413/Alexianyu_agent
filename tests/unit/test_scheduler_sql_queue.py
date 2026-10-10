"""SQL scheduler queue producer idempotency and consumer isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.infrastructure.database.scheduler_queue import SqlAlchemyDueQueue


@pytest.fixture
async def scheduler_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "scheduler.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_enqueue_once_and_prefix_scoped_claim(scheduler_db: None) -> None:
    now = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)
    producer = SqlAlchemyDueQueue()

    assert await producer.enqueue_once(
        work_id="platform-automation:one",
        payload={"kind": "platform_automation"},
        available_at=now,
    )
    assert not await producer.enqueue_once(
        work_id="platform-automation:one",
        payload={"kind": "duplicate"},
        available_at=now,
    )
    await producer.enqueue(
        work_id="another-business:one",
        payload={"kind": "another_business"},
        available_at=now,
    )

    scoped = SqlAlchemyDueQueue(work_id_prefix="platform-automation:")
    claimed = await scoped.claim_due(
        now=now,
        lease_owner="replica-a",
        lease_duration=timedelta(minutes=1),
        limit=10,
    )

    assert [work.work_id for work in claimed] == ["platform-automation:one"]
    assert claimed[0].payload == {"kind": "platform_automation"}

    other = await producer.claim_due(
        now=now,
        lease_owner="replica-b",
        lease_duration=timedelta(minutes=1),
        limit=10,
    )
    assert [work.work_id for work in other] == ["another-business:one"]
