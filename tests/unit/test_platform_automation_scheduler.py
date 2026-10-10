"""Platform automation scheduler stays gated, durable and idempotent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from xianyu_agent.application.automation import (
    ACTION_POLISH,
    ACTION_REQUEST_FLOWER,
    PlatformAutomationScheduler,
    PlatformAutomationService,
)
from xianyu_agent.application.automation.platform_scheduler import (
    WORK_KIND,
    automation_work_id,
    enqueue_immediate_run,
    next_occurrence,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import (
    Account,
    Item,
    Order,
    PlatformAutomationRun,
    database as db_mod,
    get_async_session,
)
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.infrastructure.database.models.scheduler import SchedulerWork
from xianyu_agent.infrastructure.database.scheduler_queue import SqlAlchemyDueQueue


@dataclass(slots=True)
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


class FakePacer:
    def __init__(self) -> None:
        self.accounts: list[str] = []

    async def acquire(self, account_id: str) -> float:
        self.accounts.append(account_id)
        return 0.0


class FakePolishClient:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail_first = fail_first

    async def polish_item(self, account_id: str, item_id: str):
        self.calls.append((account_id, item_id))
        if self._fail_first and len(self.calls) == 1:
            raise RuntimeError("transport")
        return SimpleNamespace(already_polished=False)


class FakeFlowerClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    async def request_flower(self, account_id: str, order_id: str):
        self.calls.append((account_id, order_id))
        if self._fail:
            raise RuntimeError("unknown outcome")
        return SimpleNamespace(requested=True)


@pytest.fixture
async def automation_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "automation-scheduler.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1")
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def test_next_occurrence_uses_beijing_time_and_moves_out_of_quiet_hours() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 21, 18, 30, tzinfo=UTC)  # 北京 02:30

    regular = next_occurrence(
        now, schedule_time="03:00", timezone=timezone, quiet_hours=None
    )
    shifted = next_occurrence(
        now, schedule_time="03:00", timezone=timezone, quiet_hours="23:00-08:00"
    )

    assert regular == datetime(2026, 9, 21, 19, 0, tzinfo=UTC)
    assert shifted == datetime(2026, 9, 22, 0, 0, tzinfo=UTC)


def test_automation_work_id_is_deterministic_and_fits_all_backends() -> None:
    account_id = "a" * 64
    first = automation_work_id(account_id, "request_flower", "20260921T1000")
    second = automation_work_id(account_id, "request_flower", "20260921T1000")

    assert first == second
    assert len(first) <= 64
    assert first.startswith("platform-automation:")
    assert first != automation_work_id(account_id, "request_flower", "20260922T1000")


@pytest.mark.asyncio
async def test_observe_mode_neither_queries_nor_seeds_work(automation_db: None) -> None:
    scheduler = PlatformAutomationScheduler(
        instance_id="replica-a",
        active=False,
        clock=FakeClock(datetime(2026, 9, 21, 1, 0, tzinfo=UTC)),
    )

    result = await scheduler.run_once()

    assert result.enabled is False
    async with get_async_session() as session:
        assert list((await session.scalars(select(SchedulerWork))).all()) == []


@pytest.mark.asyncio
async def test_enabled_calibrated_action_seeds_only_one_future_job(
    automation_db: None,
) -> None:
    service = PlatformAutomationService()
    await service.approve_calibration("acc-1", ACTION_POLISH, approved_by="operator")
    await service.configure(
        "acc-1", ACTION_POLISH, enabled=True, schedule_time="03:00"
    )
    clock = FakeClock(datetime(2026, 9, 21, 1, 0, tzinfo=UTC))  # 北京 09:00
    scheduler = PlatformAutomationScheduler(
        instance_id="replica-a", active=True, service=service, clock=clock
    )

    first = await scheduler.run_once()
    second = await scheduler.run_once()

    assert first.seeded == 1
    assert second.seeded == 0
    async with get_async_session() as session:
        rows = list((await session.scalars(select(SchedulerWork))).all())
    assert len(rows) == 1
    assert rows[0].available_at.replace(tzinfo=UTC) == datetime(
        2026, 9, 21, 19, 0, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_due_work_inside_quiet_hours_completes_without_external_call(
    automation_db: None,
) -> None:
    service = PlatformAutomationService()
    await service.approve_calibration(
        "acc-1", ACTION_REQUEST_FLOWER, approved_by="operator"
    )
    await service.configure("acc-1", ACTION_REQUEST_FLOWER, enabled=True)
    now = datetime(2026, 9, 21, 20, 0, tzinfo=UTC)  # 北京 04:00
    queue = SqlAlchemyDueQueue(work_id_prefix="platform-automation:")
    await queue.enqueue(
        work_id="platform-automation:quiet",
        payload={
            "kind": WORK_KIND,
            "account_id": "acc-1",
            "action": ACTION_REQUEST_FLOWER,
        },
        available_at=now,
    )
    client = FakeFlowerClient()
    scheduler = PlatformAutomationScheduler(
        instance_id="replica-a",
        active=True,
        service=service,
        queue=queue,
        clock=FakeClock(now),
        pacer=FakePacer(),  # type: ignore[arg-type]
        flower_client=client,
    )

    result = await scheduler.run_once()

    assert result.runner.completed == 1
    assert result.runner.released == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_run_now_requires_active_mode_and_calibration(automation_db: None) -> None:
    service = PlatformAutomationService()
    clock = FakeClock(datetime(2026, 9, 21, 1, 0, tzinfo=UTC))

    with pytest.raises(ValueError, match="不是 active"):
        await enqueue_immediate_run(
            account_id="acc-1", action=ACTION_POLISH, active=False, clock=clock, service=service
        )
    with pytest.raises(ValueError, match="还没有"):
        await enqueue_immediate_run(
            account_id="acc-1", action=ACTION_POLISH, active=True, clock=clock, service=service
        )

    await service.approve_calibration("acc-1", ACTION_POLISH, approved_by="operator")
    with pytest.raises(ValueError, match="停用状态"):
        await enqueue_immediate_run(
            account_id="acc-1", action=ACTION_POLISH, active=True, clock=clock, service=service
        )


@pytest.mark.asyncio
async def test_run_now_enqueues_the_same_durable_work(automation_db: None) -> None:
    service = PlatformAutomationService()
    await service.approve_calibration("acc-1", ACTION_POLISH, approved_by="operator")
    await service.configure("acc-1", ACTION_POLISH, enabled=True)
    clock = FakeClock(datetime(2026, 9, 21, 1, 0, tzinfo=UTC))  # 北京 09:00
    queue = SqlAlchemyDueQueue(work_id_prefix="platform-automation:")

    work_id = await enqueue_immediate_run(
        account_id="acc-1",
        action=ACTION_POLISH,
        active=True,
        clock=clock,
        service=service,
        queue=queue,
    )

    # 同一个入口再来一次(同一秒)不会产生第二条任务:幂等仍然由数据库唯一键保证。
    again = await enqueue_immediate_run(
        account_id="acc-1",
        action=ACTION_POLISH,
        active=True,
        clock=clock,
        service=service,
        queue=queue,
    )
    assert again == work_id
    async with get_async_session() as session:
        rows = list((await session.scalars(select(SchedulerWork))).all())
    assert len(rows) == 1
    assert rows[0].payload["manual"] is True
    assert rows[0].available_at.replace(tzinfo=UTC) == clock.now()


@pytest.mark.asyncio
async def test_run_now_refuses_inside_quiet_hours(automation_db: None) -> None:
    service = PlatformAutomationService()
    await service.approve_calibration(
        "acc-1", ACTION_REQUEST_FLOWER, approved_by="operator"
    )
    await service.configure("acc-1", ACTION_REQUEST_FLOWER, enabled=True)
    clock = FakeClock(datetime(2026, 9, 21, 20, 0, tzinfo=UTC))  # 北京 04:00,静默中

    with pytest.raises(ValueError, match="静默时段"):
        await enqueue_immediate_run(
            account_id="acc-1",
            action=ACTION_REQUEST_FLOWER,
            active=True,
            clock=clock,
            service=service,
        )


@pytest.mark.asyncio
async def test_polish_uncertain_outcome_is_not_retried(
    automation_db: None,
) -> None:
    service = PlatformAutomationService()
    await service.approve_calibration("acc-1", ACTION_POLISH, approved_by="operator")
    await service.configure("acc-1", ACTION_POLISH, enabled=True)
    async with get_async_session() as session, session.begin():
        account = await session.scalar(select(Account).where(Account.account_id == "acc-1"))
        assert account is not None
        session.add(
            Item(
                account_id=account.id,
                item_id="item-1",
                title="test",
                is_on_sale=True,
            )
        )
    now = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    queue = SqlAlchemyDueQueue(work_id_prefix="platform-automation:")
    await queue.enqueue(
        work_id="platform-automation:acc-1:polish:due",
        payload={"kind": WORK_KIND, "account_id": "acc-1", "action": ACTION_POLISH},
        available_at=now,
    )
    client = FakePolishClient(fail_first=True)
    pacer = FakePacer()
    scheduler = PlatformAutomationScheduler(
        instance_id="replica-a",
        active=True,
        service=service,
        queue=queue,
        clock=FakeClock(now),
        pacer=pacer,  # type: ignore[arg-type]
        polish_client=client,
    )

    result = await scheduler.run_once()

    assert result.runner.completed == 1
    assert client.calls == [("acc-1", "item-1")]
    assert pacer.accounts == ["acc-1"]
    runs = await service.list_runs("acc-1")
    assert [(run.action, run.target_id, run.status, run.result) for run in runs] == [
        (ACTION_POLISH, "item-1", "uncertain", "RuntimeError")
    ]


@pytest.mark.asyncio
async def test_flower_uses_calibration_cutover_and_never_retries_uncertain_outcome(
    automation_db: None,
) -> None:
    service = PlatformAutomationService()
    approved = await service.approve_calibration(
        "acc-1", ACTION_REQUEST_FLOWER, approved_by="operator"
    )
    await service.configure(
        "acc-1",
        ACTION_REQUEST_FLOWER,
        enabled=True,
        delay_minutes=60,
        quiet_hours="00:00-00:01",
    )
    approved_at = approved.calibration_approved_at
    assert approved_at is not None
    approved_utc = (
        approved_at.replace(tzinfo=UTC)
        if approved_at.tzinfo is None
        else approved_at.astimezone(UTC)
    )
    now = approved_utc + timedelta(hours=12)
    async with get_async_session() as session, session.begin():
        account = await session.scalar(select(Account).where(Account.account_id == "acc-1"))
        assert account is not None
        session.add_all(
            [
                Order(
                    account_id=account.id,
                    order_id="old-order",
                    status="completed",
                    delivered_at=approved_utc - timedelta(days=1),
                ),
                Order(
                    account_id=account.id,
                    order_id="new-order",
                    status="completed",
                    delivered_at=approved_utc + timedelta(hours=1),
                ),
            ]
        )
    queue = SqlAlchemyDueQueue(work_id_prefix="platform-automation:")
    await queue.enqueue(
        work_id="platform-automation:acc-1:request_flower:due",
        payload={
            "kind": WORK_KIND,
            "account_id": "acc-1",
            "action": ACTION_REQUEST_FLOWER,
        },
        available_at=now,
    )
    client = FakeFlowerClient(fail=True)
    scheduler = PlatformAutomationScheduler(
        instance_id="replica-a",
        active=True,
        service=service,
        queue=queue,
        clock=FakeClock(now),
        pacer=FakePacer(),  # type: ignore[arg-type]
        flower_client=client,
    )

    first = await scheduler.run_once()
    await queue.enqueue(
        work_id="platform-automation:acc-1:request_flower:due-again",
        payload={
            "kind": WORK_KIND,
            "account_id": "acc-1",
            "action": ACTION_REQUEST_FLOWER,
        },
        available_at=now,
    )
    second = await scheduler.run_once()

    assert first.runner.completed == 1
    assert second.runner.completed == 1
    assert client.calls == [("acc-1", "new-order")]
    async with get_async_session() as session:
        runs = list((await session.scalars(select(PlatformAutomationRun))).all())
    assert [(run.target_id, run.status, run.result) for run in runs] == [
        ("new-order", "uncertain", "RuntimeError")
    ]
