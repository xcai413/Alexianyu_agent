"""FAIL_SYS_USER_VALIDATE 持久化熔断测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, worker_commands, worker_risk
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.ws_auth import WsAuthError


@pytest.fixture
async def risk_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "worker-risk.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("risk-account", enabled=True)
    await domain_accounts.create_account("healthy-account", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_open_circuit_stops_only_risk_account_and_writes_audit(risk_db) -> None:
    _ = risk_db
    await domain_accounts.set_desired_state("risk-account", "running")
    await domain_accounts.set_desired_state("healthy-account", "running")
    now = datetime(2026, 8, 21, 4, 0, tzinfo=UTC)

    circuit = await worker_risk.open_user_validate("risk-account", now=now)

    assert circuit is not None
    assert circuit.code == "FAIL_SYS_USER_VALIDATE"
    assert circuit.cooldown_until == now + timedelta(minutes=20)
    risk_account = await domain_accounts.get_account("risk-account")
    healthy_account = await domain_accounts.get_account("healthy-account")
    assert risk_account is not None
    assert healthy_account is not None
    assert risk_account.desired_state == "stopped"
    assert healthy_account.desired_state == "running"
    status = await domain_accounts.worker_status_for("risk-account")
    assert status is not None
    assert status.status == "risk_cooling"
    assert status.risk_code == "FAIL_SYS_USER_VALIDATE"
    assert status.risk_recovery_required is True
    async with get_async_session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "ws_user_validate_circuit_opened")
            )
        ).scalar_one()
    assert audit.target == "risk-account"
    assert audit.error == "FAIL_SYS_USER_VALIDATE"


@pytest.mark.asyncio
async def test_circuit_blocks_start_until_successful_manual_refresh(risk_db) -> None:
    _ = risk_db
    await worker_risk.open_user_validate("risk-account")

    with pytest.raises(ValueError, match="验证冷却"):
        await worker_commands.submit("risk-account", "start")
    valid_commands = await worker_commands.submit_many("start")
    assert [await worker_commands.account_key(command) for command in valid_commands] == [
        "healthy-account"
    ]

    assert await worker_risk.clear_after_refresh("risk-account") is True
    command = await worker_commands.submit("risk-account", "start")
    assert command is not None
    status = await domain_accounts.worker_status_for("risk-account")
    assert status is not None
    assert status.risk_recovery_required is False
    assert status.risk_code is None


@pytest.mark.asyncio
async def test_ws_user_validate_stops_retry_loop_and_preserves_risk_state(risk_db) -> None:
    _ = risk_db

    class UserValidateProvider:
        async def get_credentials(self, _account_id: str):
            raise WsAuthError("账号 risk-account WS Token 请求失败:FAIL_SYS_USER_VALIDATE")

    async def open_circuit(error: WsAuthError) -> bool:
        assert worker_risk.is_user_validate_error(error)
        await worker_risk.open_user_validate("risk-account")
        return True

    client = WsClient(
        "risk-account",
        config=ClientConfig(
            ws_url="ws://unused",
            auth_retry_delay_s=0.001,
            min_backoff_s=0.001,
            max_backoff_s=0.001,
        ),
        token_provider=UserValidateProvider(),
        on_auth_failure=open_circuit,
    )

    await client._run_forever()

    assert client._reconnect_attempts == 1
    account = await domain_accounts.get_account("risk-account")
    status = await domain_accounts.worker_status_for("risk-account")
    assert account is not None
    assert status is not None
    assert account.desired_state == "stopped"
    assert status.status == "risk_cooling"
    assert status.last_error == "FAIL_SYS_USER_VALIDATE"
    assert status.risk_recovery_required is True


@pytest.mark.asyncio
async def test_circuit_status_reports_expired_cooldown_but_keeps_recovery_gate(risk_db) -> None:
    _ = risk_db
    now = datetime(2026, 8, 21, 4, 0, tzinfo=UTC)
    await worker_risk.open_user_validate("risk-account", now=now)

    circuit = await worker_risk.get("risk-account")

    assert circuit is not None
    assert circuit.is_cooling(now=now + timedelta(minutes=19)) is True
    assert circuit.is_cooling(now=now + timedelta(minutes=20)) is False
    assert circuit.remaining_seconds(now=now + timedelta(minutes=25)) == 0.0
