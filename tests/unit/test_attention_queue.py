"""待人工处理队列:聚合、只读与命令提示。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.application.attention import AttentionService
from xianyu_agent.application.automation import PlatformAutomationService
from xianyu_agent.application.chat import ChatAssistService
from xianyu_agent.application.delivery import DeliveryJobService
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, messages as domain_messages
from xianyu_agent.domain.delivery import DeliveryJobState
from xianyu_agent.domain.events import MessageReceived
from xianyu_agent.infrastructure.database.models import (
    Account as AccountRow,
    WorkerStatus as WorkerStatusRow,
)
from xianyu_agent.services.logging_setup import summarize_log_levels

runner = CliRunner()


@pytest.fixture
async def attention_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "attention.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def test_queue_is_empty_on_a_fresh_database(attention_db: Path) -> None:
    assert await AttentionService().list_items() == []


async def test_message_notifications_apply_skip_notify_filters(attention_db: Path) -> None:
    now = datetime.now(UTC)
    first = MessageReceived(
        event_id="evt-message-1",
        account_id="acc-1",
        received_at=now,
        chat_id="chat-1",
        message_id="msg-1",
        sender_id="buyer-1",
        content="普通咨询",
    )
    second = MessageReceived(
        event_id="evt-message-2",
        account_id="acc-1",
        received_at=now,
        chat_id="chat-2",
        message_id="msg-2",
        sender_id="buyer-2",
        content="请勿通知关键词",
    )
    await domain_messages.upsert_inbound(first)
    await domain_messages.upsert_inbound(second)
    await ChatAssistService().create_message_filter(
        account_id="acc-1", keyword="勿通知", filter_type="skip_notify"
    )

    items = await AttentionService().list_message_notifications()

    assert len(items) == 1
    assert items[0].kind == "message_received"
    assert items[0].account_id == "acc-1"
    assert "message show" in items[0].next_command


async def test_uncertain_delivery_job_shows_up(attention_db: Path) -> None:
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-9")
    assert job.local_id is not None
    await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    await service.set_state(job.local_id, DeliveryJobState.DELIVERING.value)
    await service.set_state(job.local_id, DeliveryJobState.UNCERTAIN.value)

    items = await AttentionService().list_items()

    assert [item.kind for item in items] == ["delivery_job"]
    assert items[0].target == "acc-1/O-9"
    assert items[0].state == DeliveryJobState.UNCERTAIN.value
    # 提示必须直接给出收尾命令:只说"去看一眼"会让运营自己找命令,而这条命令曾经挂错实现。
    assert f"delivery job resolve {job.local_id}" in items[0].next_command
    assert "--delivered" in items[0].next_command


async def test_uncertain_automation_run_shows_up_with_a_resolution_command(
    attention_db: Path,
) -> None:
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        "request_flower",
        target_id="order-1",
        idempotency_key="request_flower:order-1",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="BuyerFlowerError")

    items = await AttentionService().list_items()

    assert [item.kind for item in items] == ["automation_run"]
    assert items[0].state == "uncertain"
    assert f"--run-id {run_id}" in items[0].next_command
    assert items[0].account_id == "acc-1"


async def test_resolved_runs_leave_the_queue(attention_db: Path) -> None:
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        "request_flower",
        target_id="order-2",
        idempotency_key="request_flower:order-2",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="transport")
    await service.resolve_run(
        run_id, status="completed", note="平台已确认发货", resolved_by="operator"
    )

    assert await AttentionService().list_items() == []


async def test_account_needing_validation_shows_up(attention_db: Path) -> None:
    async with get_async_session() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == "acc-1").limit(1)
            )
        ).scalar_one()
        session.add(
            WorkerStatusRow(
                account_id=account.id,
                status="needs_validation",
                risk_code="FAIL_SYS_USER_VALIDATE",
                risk_recovery_required=True,
            )
        )

    items = await AttentionService().list_items()

    assert [item.kind for item in items] == ["account_risk"]
    assert items[0].target == "acc-1"
    assert "auth refresh" in items[0].next_command


def test_cli_lists_the_queue(attention_db: Path) -> None:
    async def seed() -> None:
        service = PlatformAutomationService()
        run_id = await service.reserve_run(
            "acc-1",
            "request_flower",
            target_id="order-3",
            idempotency_key="request_flower:order-3",
        )
        assert run_id is not None
        await service.finish_run(run_id, status="uncertain", result="transport")

    asyncio.run(seed())
    result = runner.invoke(app, ["attention", "list"])

    assert result.exit_code == 0, result.output
    assert "平台自动化" in result.output
    assert "automation resolve" in result.output


def test_log_level_summary_counts_without_copying_content() -> None:
    lines = [
        "2026-09-21 10:00:00 INFO daemon started",
        "2026-09-21 10:00:01 WARNING 风控命中",
        "2026-09-21 10:00:02 ERROR 任务失败 cookie=secret-value",
        "random line without a level",
    ]

    counts = summarize_log_levels(lines)

    assert counts == {
        "DEBUG": 0,
        "INFO": 1,
        "WARNING": 1,
        "ERROR": 1,
        "CRITICAL": 0,
        "OTHER": 1,
    }
    assert "secret-value" not in str(counts)
