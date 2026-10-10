"""Platform automation scheduling contract on each supported SQL backend."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from xianyu_agent.application.automation import (
    ACTION_POLISH,
    PlatformAutomationScheduler,
    PlatformAutomationService,
)
from xianyu_agent.application.automation.platform_scheduler import WORK_KIND, WORK_PREFIX
from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, Item, PlatformAutomationRun, get_async_session
from xianyu_agent.infrastructure.database.models.scheduler import SchedulerWork
from xianyu_agent.infrastructure.database.scheduler_queue import SqlAlchemyDueQueue


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class ImmediatePacer:
    async def acquire(self, _account_id: str) -> float:
        return 0.0


class RecordingPolishClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def polish_item(self, account_id: str, item_id: str):
        self.calls.append((account_id, item_id))
        return SimpleNamespace(already_polished=False)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_platform_scheduler_executes_once_across_backend() -> None:
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend

    suffix = uuid4().hex[:12]
    account_id = f"automation-{backend}-{suffix}"
    work_id = f"{WORK_PREFIX}{uuid4().hex}"
    now = datetime.now(UTC)
    try:
        async with get_async_session() as session, session.begin():
            account = Account(account_id=account_id, enabled=True)
            session.add(account)
            await session.flush()
            session.add(
                Item(
                    account_id=account.id,
                    item_id=f"item-{suffix}",
                    title="backend test",
                    is_on_sale=True,
                )
            )
        service = PlatformAutomationService()
        await service.approve_calibration(account_id, ACTION_POLISH, approved_by="test")
        await service.configure(account_id, ACTION_POLISH, enabled=True)
        queue = SqlAlchemyDueQueue(work_id_prefix=WORK_PREFIX)
        await queue.enqueue(
            work_id=work_id,
            payload={
                "kind": WORK_KIND,
                "account_id": account_id,
                "action": ACTION_POLISH,
            },
            available_at=now,
        )
        client = RecordingPolishClient()
        scheduler = PlatformAutomationScheduler(
            instance_id=f"replica-{suffix}",
            active=True,
            service=service,
            queue=queue,
            clock=FixedClock(now),
            pacer=ImmediatePacer(),  # type: ignore[arg-type]
            polish_client=client,
        )

        first = await scheduler.run_once()
        second = await scheduler.run_once()

        assert first.runner.completed == 1
        assert second.runner.completed == 0
        assert client.calls == [(account_id, f"item-{suffix}")]
        async with get_async_session() as session:
            runs = list(
                (
                    await session.scalars(
                        select(PlatformAutomationRun).join(
                            Account, Account.id == PlatformAutomationRun.account_id
                        ).where(Account.account_id == account_id)
                    )
                ).all()
            )
        assert [(row.status, row.result) for row in runs] == [("completed", "polished")]
    finally:
        async with get_async_session() as session, session.begin():
            await session.execute(
                delete(SchedulerWork).where(
                    SchedulerWork.work_id.startswith(WORK_PREFIX, autoescape=True)
                )
            )
            await session.execute(delete(Account).where(Account.account_id == account_id))
