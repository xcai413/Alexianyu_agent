"""擦亮/求花自动化在真实校准前必须 fail closed。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from xianyu_agent.application.automation import (
    ACTION_POLISH,
    ACTION_REQUEST_FLOWER,
    PlatformAutomationService,
)
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import PlatformAutomationRun, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts

runner = CliRunner()


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "automation.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(db_mod.init_db())
    asyncio.run(domain_accounts.create_account("acc-1"))
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


async def test_enable_requires_explicit_real_calibration(wired_db: Path) -> None:
    service = PlatformAutomationService()

    with pytest.raises(ValueError, match="尚未完成真实校准"):
        await service.configure("acc-1", ACTION_POLISH, enabled=True)

    approved = await service.approve_calibration(
        "acc-1", ACTION_POLISH, approved_by="operator"
    )
    assert approved.calibrated is True
    assert approved.enabled is False

    enabled = await service.configure(
        "acc-1", ACTION_POLISH, enabled=True, schedule_time="03:00"
    )
    assert enabled.enabled is True
    assert enabled.schedule_time == "03:00"

    revoked = await service.revoke_calibration("acc-1", ACTION_POLISH)
    assert revoked.calibrated is False
    assert revoked.enabled is False


async def test_request_flower_defaults_to_delay_and_quiet_hours_are_validated(
    wired_db: Path,
) -> None:
    service = PlatformAutomationService()
    approved = await service.approve_calibration(
        "acc-1", ACTION_REQUEST_FLOWER, approved_by="operator"
    )
    assert approved.delay_minutes == 24 * 60
    assert approved.quiet_hours == "23:00-08:00"

    configured = await service.configure(
        "acc-1",
        ACTION_REQUEST_FLOWER,
        enabled=True,
        quiet_hours="23:00-08:00",
    )
    assert configured.quiet_hours == "23:00-08:00"
    with pytest.raises(ValueError, match="HH:MM-HH:MM"):
        await service.configure(
            "acc-1", ACTION_REQUEST_FLOWER, enabled=False, quiet_hours="night"
        )


async def test_seller_features_is_a_projection_of_existing_automation(wired_db: Path) -> None:
    service = PlatformAutomationService()
    await service.approve_calibration("acc-1", ACTION_REQUEST_FLOWER, approved_by="operator")
    await service.configure("acc-1", ACTION_REQUEST_FLOWER, enabled=True)

    features = await service.seller_features("acc-1")

    assert features.request_flower_enabled is True
    assert features.polish_enabled is False
    assert features.auto_rate_enabled is False
    assert features.auto_thanks_enabled is False
    assert features.unsupported == ("auto_rate", "auto_thanks")
    with pytest.raises(ValueError, match="不能相同"):
        await service.configure(
            "acc-1",
            ACTION_REQUEST_FLOWER,
            enabled=False,
            quiet_hours="00:00-00:00",
        )


async def test_run_reservation_is_idempotent_and_records_terminal_state(
    wired_db: Path,
) -> None:
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        ACTION_REQUEST_FLOWER,
        target_id="order-1",
        idempotency_key="request_flower:order-1",
    )
    assert run_id is not None
    duplicate = await service.reserve_run(
        "acc-1",
        ACTION_REQUEST_FLOWER,
        target_id="order-1",
        idempotency_key="request_flower:order-1",
    )
    assert duplicate is None

    await service.finish_run(run_id, status="uncertain", result="transport outcome unknown")
    async with get_async_session() as session:
        row = await session.get(PlatformAutomationRun, run_id)
    assert row is not None
    assert row.status == "uncertain"
    assert row.finished_at is not None


def test_cli_keeps_calibration_and_enable_as_separate_steps(wired_db: Path) -> None:
    blocked = runner.invoke(
        app,
        ["automation", "set", "-a", "acc-1", "--action", "polish", "--enabled"],
    )
    assert blocked.exit_code == 1
    assert "尚未完成真实校准" in blocked.output

    approved = runner.invoke(
        app,
        [
            "automation",
            "calibration",
            "approve",
            "-a",
            "acc-1",
            "--action",
            "polish",
            "--yes",
        ],
    )
    assert approved.exit_code == 0, approved.output
    assert "仍为 disabled" in approved.output

    enabled = runner.invoke(
        app,
        ["automation", "set", "-a", "acc-1", "--action", "polish", "--enabled"],
    )
    assert enabled.exit_code == 0, enabled.output
    status = runner.invoke(app, ["automation", "status", "-a", "acc-1"])
    assert status.exit_code == 0
    assert "polish" in status.output


def test_cli_lists_automation_run_outcomes(wired_db: Path) -> None:
    async def seed() -> None:
        service = PlatformAutomationService()
        run_id = await service.reserve_run(
            "acc-1",
            ACTION_REQUEST_FLOWER,
            target_id="order-1",
            idempotency_key="request_flower:order-1",
        )
        assert run_id is not None
        await service.finish_run(run_id, status="uncertain", result="BuyerFlowerError")

    asyncio.run(seed())
    result = runner.invoke(app, ["automation", "runs", "-a", "acc-1"])

    assert result.exit_code == 0, result.output
    assert "request_flower" in result.output
    assert "order-1" in result.output
    assert "uncertain" in result.output


async def test_manual_resolution_only_accepts_uncertain_runs(wired_db: Path) -> None:
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        ACTION_REQUEST_FLOWER,
        target_id="order-1",
        idempotency_key="request_flower:order-1",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="BuyerFlowerError")

    resolved = await service.resolve_run(
        run_id,
        status="completed",
        note="平台订单页已确认发货",
        resolved_by="operator",
    )

    assert resolved.status == "completed"
    assert "人工收尾" in (resolved.result or "")

    with pytest.raises(ValueError, match="不是结果不明"):
        await service.resolve_run(
            run_id, status="failed", note="再收尾一次", resolved_by="operator"
        )


async def test_manual_resolution_requires_a_reason(wired_db: Path) -> None:
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        ACTION_REQUEST_FLOWER,
        target_id="order-2",
        idempotency_key="request_flower:order-2",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="transport")

    with pytest.raises(ValueError, match="必须写明依据"):
        await service.resolve_run(
            run_id, status="completed", note="   ", resolved_by="operator"
        )
    with pytest.raises(ValueError, match="completed 或 failed"):
        await service.resolve_run(
            run_id, status="skipped", note="随便标一个", resolved_by="operator"
        )


def test_cli_resolves_an_uncertain_run(wired_db: Path) -> None:
    async def seed() -> int:
        service = PlatformAutomationService()
        run_id = await service.reserve_run(
            "acc-1",
            ACTION_REQUEST_FLOWER,
            target_id="order-3",
            idempotency_key="request_flower:order-3",
        )
        assert run_id is not None
        await service.finish_run(run_id, status="uncertain", result="transport")
        return run_id

    run_id = asyncio.run(seed())
    result = runner.invoke(
        app,
        [
            "automation",
            "resolve",
            "--run-id",
            str(run_id),
            "--status",
            "failed",
            "--note",
            "平台订单页未发货",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "已收尾为 failed" in result.output
