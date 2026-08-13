"""P0.4 doctor 报告与 CLI 测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from xianyu_agent.cli.main import app as cli_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Cookie, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.services import doctor as doctor_service
from xianyu_agent.services.daemon_health import DaemonHealth
from xianyu_agent.services.doctor import DiagnosticCheck, DoctorReport


@pytest.fixture
async def doctor_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "doctor.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_cookie_check_warns_for_disabled_stale_cookie(doctor_db: Path) -> None:
    await domain_accounts.create_account("disabled", enabled=False)
    account = await domain_accounts.get_account("disabled")
    assert account is not None
    async with db_mod.get_async_session() as session:
        session.add(Cookie(account_id=account.id, encrypted_value="invalid"))
        await session.commit()

    checks = await doctor_service._check_account_cookies()

    assert checks[0].status == doctor_service.WARN
    assert "停用账号" in checks[0].summary


@pytest.mark.asyncio
async def test_cookie_check_passes_decryptable_enabled_account(doctor_db: Path) -> None:
    await domain_accounts.create_account("enabled")
    assert await CookieSigner().save_cookie("enabled", "unb=1; _m_h5_tk=seed_1") is True

    checks = await doctor_service._check_account_cookies()

    assert checks[0].status == doctor_service.PASS


def test_doctor_report_exit_code_only_fails_on_fail() -> None:
    warning = DoctorReport((DiagnosticCheck("x", "warn", "warning"),))
    failure = DoctorReport((DiagnosticCheck("x", "fail", "failure"),))
    assert warning.exit_code == 0
    assert failure.exit_code == 1


@pytest.mark.asyncio
async def test_daemon_check_warns_on_orphan_record_alongside_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    healthy = SimpleNamespace(instance_id="healthy", pid=1)
    orphan = SimpleNamespace(instance_id="orphan", pid=2)

    async def rows():
        return [healthy, orphan]

    monkeypatch.setattr(doctor_service.daemon_domain, "active_instances", rows)
    monkeypatch.setattr(
        doctor_service,
        "observe_daemon",
        lambda row: DaemonHealth(
            "online" if row is healthy else "dead",
            True,
            row is healthy,
            1.0,
            row is healthy,
        ),
    )
    checks = await doctor_service._check_daemon_instances()
    assert checks[0].status == doctor_service.WARN
    assert "遗留活跃记录" in checks[0].summary


def test_doctor_cli_json_and_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    from xianyu_agent.cli.commands import doctor as doctor_cli  # noqa: PLC0415

    async def report() -> DoctorReport:
        return DoctorReport((DiagnosticCheck("db", "fail", "broken"),))

    monkeypatch.setattr(doctor_cli, "run_doctor", report)
    result = CliRunner().invoke(cli_app, ["doctor", "--output", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["checks"][0]["name"] == "db"
