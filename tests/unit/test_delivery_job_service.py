"""发货任务应用层与 CLI 接线(Issue #5 Phase 6, 6E)。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from typer.testing import CliRunner

from xianyu_agent.application.delivery import DeliveryJobService
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryReservationService,
    InventoryService,
)
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, database as db_mod
from xianyu_agent.domain.delivery import (
    DeliveryJobState,
    FailureKind,
    InvalidJobTransition,
)
from xianyu_agent.infrastructure.database.models import DeliveryJob as DeliveryJobRow

runner = CliRunner()


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(db_mod.init_db())
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


async def _row_count() -> int:
    async with db_mod.async_session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(DeliveryJobRow))
    return int(total or 0)


async def test_create_or_get_converges_duplicate_events(wired_db: Path) -> None:
    service = DeliveryJobService()

    first = await service.create_or_get(account_id="acc-1", order_id="O-1")
    second = await service.create_or_get(account_id="acc-1", order_id="O-1")

    assert first.local_id == second.local_id
    assert await _row_count() == 1
    assert first.state == DeliveryJobState.PENDING.value


async def test_semantic_key_separates_jobs_of_the_same_order(wired_db: Path) -> None:
    service = DeliveryJobService()

    primary = await service.create_or_get(account_id="acc-1", order_id="O-1")
    other = await service.create_or_get(
        account_id="acc-1", order_id="O-1", semantic_key="order_paid:item:2"
    )

    assert primary.local_id != other.local_id
    assert await _row_count() == 2


async def test_attempts_are_numbered_in_order(wired_db: Path) -> None:
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-1")
    assert job.local_id is not None

    await service.record_attempt(job.local_id, action="reserve", result="success")
    await service.record_attempt(job.local_id, action="send", result="success")
    attempts = await service.attempts(job.local_id)

    assert [attempt.attempt_no for attempt in attempts] == [1, 2]
    assert [attempt.action for attempt in attempts] == ["reserve", "send"]


async def test_failure_classification_drives_the_job_state(wired_db: Path) -> None:
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-1")
    assert job.local_id is not None
    await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    reserved = await service.set_state(job.local_id, DeliveryJobState.RESERVED.value)

    assert reserved.reservation_id is None

    retryable = await service.register_failure(
        job.local_id, FailureKind.NOT_SENT, error="连接建立前失败"
    )

    assert retryable.state == DeliveryJobState.FAILED_RETRYABLE.value
    assert retryable.last_error == "连接建立前失败"
    attempts = await service.attempts(job.local_id)
    assert attempts[-1].result == "failed_not_sent"

    retried = await service.retry(job.local_id)
    assert retried.state == DeliveryJobState.CHECKING.value


async def test_force_retry_reopens_only_a_definite_not_sent_final_job(wired_db: Path) -> None:
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-FORCE")
    assert job.local_id is not None
    await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    for _ in range(3):
        await service.record_attempt(job.local_id, action="send", result="failed_not_sent")
    final = await service.register_failure(
        job.local_id, FailureKind.NOT_SENT, error="常驻连接拒绝一次性发送"
    )
    assert final.state == DeliveryJobState.FAILED_FINAL.value

    reopened = await service.retry(job.local_id, force=True)

    assert reopened.state == DeliveryJobState.CHECKING.value


async def test_unknown_failure_freezes_the_job(wired_db: Path) -> None:
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-1")
    assert job.local_id is not None
    await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVED.value)

    frozen = await service.register_failure(
        job.local_id, FailureKind.UNKNOWN, error="发送后连接中断"
    )

    assert frozen.state == DeliveryJobState.UNCERTAIN.value
    assert (await service.attempts(job.local_id))[-1].result == "unknown"
    with pytest.raises(InvalidJobTransition):
        await service.retry(job.local_id)

    resolved = await service.resolve_uncertain(
        job.local_id, delivered=True, note="平台订单页已确认收到卡密"
    )
    assert resolved.state == DeliveryJobState.COMPLETED.value


async def test_human_verdict_that_nothing_was_sent_allows_retry(wired_db: Path) -> None:
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-2")
    assert job.local_id is not None
    await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    await service.register_failure(job.local_id, FailureKind.UNKNOWN, error="未知")

    resolved = await service.resolve_uncertain(
        job.local_id, delivered=False, note="平台订单页没有发货记录"
    )

    assert resolved.state == DeliveryJobState.FAILED_RETRYABLE.value
    assert (await service.retry(job.local_id)).state == DeliveryJobState.CHECKING.value


async def test_attach_reservation_and_listing(wired_db: Path) -> None:
    pool = await InventoryService().create_pool(name="卡密池", type_="data")
    await InventoryImportService().import_units(pool.local_id, ["CARD-1"])
    reservation = await InventoryReservationService().reserve(pool.local_id, "job", 1)
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-3", required_quantity=2)
    assert job.local_id is not None

    attached = await service.attach_reservation(job.local_id, reservation.local_id)

    assert attached.reservation_id == reservation.local_id
    listed = await service.list(account_id="acc-1", state=DeliveryJobState.PENDING.value)
    assert [item.local_id for item in listed] == [job.local_id]
    assert await service.get_by_order("acc-1", "O-3") is not None
    assert await service.get_by_order("acc-1", "O-404") is None


def test_cli_job_list_show_retry_cancel(wired_db: Path) -> None:
    listed_empty = runner.invoke(app, ["delivery", "job", "list"])
    assert listed_empty.exit_code == 0
    assert "暂无发货任务" in listed_empty.output

    async def _seed() -> int:
        service = DeliveryJobService()
        job = await service.create_or_get(account_id="acc-1", order_id="O-1")
        assert job.local_id is not None
        await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
        await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
        await service.register_failure(job.local_id, FailureKind.NOT_SENT, error="连接失败")
        return job.local_id

    job_id = asyncio.run(_seed())

    listed = runner.invoke(app, ["delivery", "job", "list"])
    assert listed.exit_code == 0
    assert "failed_retryable" in listed.output

    shown = runner.invoke(app, ["delivery", "job", "show", str(job_id)])
    assert shown.exit_code == 0
    assert "failed_not_sent" in shown.output

    retried = runner.invoke(app, ["delivery", "job", "retry", str(job_id), "--yes"])
    assert retried.exit_code == 0
    assert "checking" in retried.output

    cancelled = runner.invoke(
        app, ["delivery", "job", "cancel", str(job_id), "--reason", "买家取消", "--yes"]
    )
    assert cancelled.exit_code == 0
    assert "已取消" in cancelled.output


async def _frozen_job_with_card(service: DeliveryJobService, order_id: str) -> tuple[int, int]:
    """造一个"卡已预留、但发送结果不明"的任务:这是人工收尾唯一该处理的状态。"""
    pool = await InventoryService().create_pool(name=f"卡密池-{order_id}", type_="data")
    await InventoryImportService().import_units(pool.local_id, [f"CARD-{order_id}"])
    reservation = await InventoryReservationService().reserve(pool.local_id, "job", 1)
    job = await service.create_or_get(account_id="acc-1", order_id=order_id)
    assert job.local_id is not None
    await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVED.value)
    await service.attach_reservation(job.local_id, reservation.local_id)
    await InventoryReservationService().mark_uncertain(reservation.local_id)
    frozen = await service.register_failure(
        job.local_id, FailureKind.UNKNOWN, error="发送后连接中断"
    )
    assert frozen.state == DeliveryJobState.UNCERTAIN.value
    return job.local_id, reservation.local_id


async def test_resolve_requires_a_written_reason(wired_db: Path) -> None:
    service = DeliveryJobService()
    job_id, _ = await _frozen_job_with_card(service, "O-NOTE")

    with pytest.raises(ValueError, match="必须写明依据"):
        await service.resolve_uncertain(job_id, delivered=True, note="   ")


async def test_resolve_only_accepts_a_frozen_job(wired_db: Path) -> None:
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-FRESH")
    assert job.local_id is not None

    with pytest.raises(ValueError, match="不是结果不明"):
        await service.resolve_uncertain(
            job.local_id, delivered=True, note="平台订单页已确认"
        )


async def test_resolve_settles_the_job_and_the_reservation_together(wired_db: Path) -> None:
    service = DeliveryJobService()
    job_id, reservation_id = await _frozen_job_with_card(service, "O-TOGETHER")

    resolved = await service.resolve_uncertain(
        job_id, delivered=True, note="平台订单页已确认收到卡密", resolved_by="test-account"
    )

    assert resolved.state == DeliveryJobState.COMPLETED.value
    assert "人工收尾(test-account)" in (resolved.last_error or "")
    reservation = await InventoryReservationService().get(reservation_id)
    assert reservation.state == "consumed"


async def test_resolve_without_delivery_releases_the_card(wired_db: Path) -> None:
    service = DeliveryJobService()
    job_id, reservation_id = await _frozen_job_with_card(service, "O-RELEASE")

    resolved = await service.resolve_uncertain(
        job_id, delivered=False, note="平台订单页没有发货记录"
    )

    assert resolved.state == DeliveryJobState.FAILED_RETRYABLE.value
    reservation = await InventoryReservationService().get(reservation_id)
    assert reservation.state == "released"


async def test_resolve_is_idempotent_for_an_already_settled_reservation(wired_db: Path) -> None:
    """任务被冻结但库存已经有人收尾过时,不能因为重复处理而报错。"""
    service = DeliveryJobService()
    job_id, reservation_id = await _frozen_job_with_card(service, "O-DOUBLE")
    await InventoryReservationService().resolve_uncertain(reservation_id, delivered=True)

    resolved = await service.resolve_uncertain(
        job_id, delivered=True, note="平台订单页已确认收到卡密"
    )

    assert resolved.state == DeliveryJobState.COMPLETED.value


async def test_resolve_writes_an_audit_row_without_the_reason_text(wired_db: Path) -> None:
    service = DeliveryJobService()
    job_id, reservation_id = await _frozen_job_with_card(service, "O-AUDIT")

    await service.resolve_uncertain(
        job_id, delivered=True, note="买家截图显示已收到", resolved_by="test-account"
    )

    async with db_mod.async_session_factory() as session:
        row = await session.scalar(
            select(AuditLog).where(AuditLog.action == "delivery.job.resolved")
        )
    assert row is not None
    assert row.actor == "cli"
    assert row.params["has_note"] is True
    assert row.params["stock"]["action"] == "consumed"
    assert row.params["stock"]["reservation_id"] == reservation_id
    # 依据正文留在任务上,不进审计参数。
    assert "买家截图" not in str(row.params)
