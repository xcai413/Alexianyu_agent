"""P0.4 doctor 报告与 CLI 测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.application.delivery import DeliveryPolicyService, DeliveryTemplateService
from xianyu_agent.application.inventory import InventoryService
from xianyu_agent.application.notify import NotificationChannelService
from xianyu_agent.application.ports.secret_vault import SecretMetadata, SecretValue
from xianyu_agent.cli.main import app as cli_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Cookie, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.infrastructure.database.models import (
    Account as AccountRow,
    PlatformAutomationConfig as PlatformAutomationConfigRow,
)
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault
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
        lambda row, **_kwargs: DaemonHealth(
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


@pytest.mark.asyncio
async def test_daemon_check_accepts_multiple_healthy_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    rows = [
        SimpleNamespace(instance_id="replica-a", pid=1),
        SimpleNamespace(instance_id="replica-b", pid=1),
    ]

    async def active():
        return rows

    monkeypatch.setattr(doctor_service.daemon_domain, "active_instances", active)
    monkeypatch.setattr(
        doctor_service,
        "observe_daemon",
        lambda _row, **_kwargs: DaemonHealth("online", True, True, 1.0, True),
    )

    checks = await doctor_service._check_daemon_instances()

    assert checks[0].status == doctor_service.PASS
    assert "2 个 daemon" in checks[0].summary


@pytest.mark.asyncio
async def test_daemon_check_warns_instead_of_blocking_startup_for_only_stale_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    row = SimpleNamespace(instance_id="stale-replica", pid=1)

    async def active():
        return [row]

    monkeypatch.setattr(doctor_service.daemon_domain, "active_instances", active)
    monkeypatch.setattr(
        doctor_service,
        "observe_daemon",
        lambda _row, **_kwargs: DaemonHealth("stale", True, False, 120.0, False),
    )

    checks = await doctor_service._check_daemon_instances()

    assert checks[0].status == doctor_service.WARN
    assert "过期记录" in checks[0].summary


@pytest.mark.asyncio
async def test_startup_doctor_allows_expected_worker_bootstrap_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    snapshot = SimpleNamespace(
        healthy_daemons=(),
        accounts=(SimpleNamespace(account_id="a"),),
        drifted_accounts=(
            SimpleNamespace(
                account_id="a",
                enabled=True,
                desired_state="running",
                actual_state="disabled",
                issue="期望 running,实际 disabled",
            ),
        ),
    )

    async def build_snapshot():
        return snapshot

    monkeypatch.setattr(doctor_service, "build_runtime_snapshot", build_snapshot)

    startup = await doctor_service._check_worker_alignment(startup=True)
    regular = await doctor_service._check_worker_alignment()

    assert startup[0].status == doctor_service.WARN
    assert "等待 daemon 启动" in startup[0].summary
    assert regular[0].status == doctor_service.FAIL


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


@pytest.mark.asyncio
async def test_database_check_accepts_remote_backend_without_sqlite_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres/MySQL 模式没有本地文件可判可写,连接探针通过即算 PASS。"""

    class _FakeResult:
        def scalar_one(self) -> int:
            return 1

    class _FakeSession:
        async def execute(self, _statement: object) -> _FakeResult:
            return _FakeResult()

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    monkeypatch.setattr(
        doctor_service,
        "engine_info",
        lambda: {
            "backend": "postgresql",
            "url": "postgresql+asyncpg://user:***@postgres:5432/xianyu",
            "db_path": None,
            "exists": None,
        },
    )
    monkeypatch.setattr(doctor_service, "get_async_session", _FakeSession)

    checks = await doctor_service._check_database()

    assert checks[0].status == doctor_service.PASS
    assert "postgresql" in checks[0].summary
    assert "***" in (checks[0].detail or "")


def test_sqlite_file_path_follows_the_effective_url() -> None:
    """显式 DATABASE_URL 指向别的 sqlite 文件时,必须按该文件判可写。"""

    assert doctor_service._sqlite_file_path("sqlite+aiosqlite:////tmp/x.db") == Path("/tmp/x.db")
    assert doctor_service._sqlite_file_path("sqlite+aiosqlite:///./data/x.db") == Path(
        "./data/x.db"
    )
    assert doctor_service._sqlite_file_path("sqlite+aiosqlite:///:memory:") is None
    assert doctor_service._sqlite_file_path("postgresql+asyncpg://user:pw@db:5432/xianyu") is None


def _status_of(checks: list[DiagnosticCheck], name: str) -> str | None:
    for check in checks:
        if check.name == name:
            return check.status
    return None


@pytest.mark.asyncio
async def test_automation_check_fails_when_enabled_without_calibration(
    doctor_db: Path,
) -> None:
    """直接改库绕过服务层校验时,doctor 必须把"启用了但没校准"报出来。"""
    await domain_accounts.create_account("acc-1", enabled=True)
    async with get_async_session() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == "acc-1").limit(1)
            )
        ).scalar_one()
        session.add(
            PlatformAutomationConfigRow(
                account_id=account.id,
                action="polish",
                enabled=True,
                schedule_time="03:00",
                delay_minutes=0,
            )
        )

    checks = await doctor_service._check_automation_config()

    assert _status_of(checks, "platform_automation") == doctor_service.FAIL


@pytest.mark.asyncio
async def test_delivery_check_fails_on_disabled_pool_and_missing_template(
    doctor_db: Path,
) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    pool = await InventoryService().create_pool(
        name="池", type_="data", enabled=False  # 建好但停用
    )
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool.local_id or 0, account_id="acc-1"
    )
    template = await DeliveryTemplateService().create(name="模板", content="卡密:{card}")
    assert template.local_id is not None
    await DeliveryTemplateService().set_enabled(template.local_id, enabled=False)
    pool2 = await InventoryService().create_pool(name="池2", type_="data")
    await DeliveryPolicyService().create(
        name="策略2",
        inventory_pool_id=pool2.local_id or 0,
        account_id="acc-1",
        template_ref="模板",
    )

    checks = await doctor_service._check_delivery_config()

    assert _status_of(checks, "delivery_policies") == doctor_service.FAIL
    assert _status_of(checks, "delivery_templates") == doctor_service.FAIL


@pytest.mark.asyncio
async def test_delivery_check_warns_when_no_policy_is_enabled(doctor_db: Path) -> None:
    checks = await doctor_service._check_delivery_config()

    assert _status_of(checks, "delivery_policies") == doctor_service.WARN


@pytest.mark.asyncio
async def test_pool_scope_pointing_at_a_missing_account_is_reported(
    doctor_db: Path,
) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await InventoryService().create_pool(name="孤儿池", type_="data", account_scope="acc-404")

    checks = await doctor_service._check_delivery_config()

    assert _status_of(checks, "pool_scope") == doctor_service.WARN


@pytest.mark.asyncio
async def test_alert_webhook_check_reports_scheme_without_echoing_the_url(
    doctor_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XIANYU_ALERT_WEBHOOK_URL", "http://alerts.example.com/hook")
    reset_settings_cache()

    checks = await doctor_service._check_automation_config()

    assert _status_of(checks, "alert_webhook") == doctor_service.WARN
    # 地址里可能带令牌,所以明细不能回显。
    assert all("alerts.example.com" not in (check.detail or "") for check in checks)


@pytest.mark.asyncio
async def test_api_binding_check_warns_only_when_the_agent_api_is_exposed(
    doctor_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent API 没有鉴权,所以只有绑定到非本机地址时才是问题。"""
    monkeypatch.delenv("XIANYU_MCP_HOST", raising=False)
    reset_settings_cache()
    default_checks = await doctor_service._check_api_binding()
    assert default_checks[0].status == doctor_service.PASS

    monkeypatch.setenv("XIANYU_MCP_HOST", "0.0.0.0")
    reset_settings_cache()
    exposed = await doctor_service._check_api_binding()

    assert exposed[0].status == doctor_service.WARN
    assert "未鉴权" in exposed[0].summary or "没有鉴权" in exposed[0].summary


@pytest.mark.asyncio
async def test_notification_channels_warn_when_nothing_is_configured(doctor_db: Path) -> None:
    checks = await doctor_service._check_notification_channels()

    assert _status_of(checks, "notification_channels") == doctor_service.WARN


@pytest.mark.asyncio
async def test_notification_channels_pass_without_a_secret(doctor_db: Path) -> None:
    await NotificationChannelService().create(
        name="运维群", kind="webhook", target="https://hooks.example.com/services/AAA"
    )

    checks = await doctor_service._check_notification_channels()

    assert _status_of(checks, "notification_channels") == doctor_service.PASS
    # 地址里可能带令牌,明细不能回显主机名。
    assert all("hooks.example.com" not in (check.detail or "") for check in checks)


@pytest.mark.asyncio
async def test_notification_channels_fail_when_the_secret_is_missing(doctor_db: Path) -> None:
    await NotificationChannelService().create(
        name="钉钉",
        kind="dingtalk",
        target="https://oapi.example.com/robot/send?access_token=SECRET",
        secret_ref="not-in-vault",
    )

    checks = await doctor_service._check_notification_channels()

    assert _status_of(checks, "notification_channels") == doctor_service.FAIL
    assert "not-in-vault" in (checks[0].detail or "")
    assert "access_token" not in (checks[0].detail or "")


@pytest.mark.asyncio
async def test_notification_channels_pass_when_the_secret_resolves(doctor_db: Path) -> None:
    vault = SqlAlchemySecretVault(FernetKeyring.from_environment())
    ref = await vault.put(SecretValue("SEC123"), metadata=SecretMetadata({"purpose": "test"}))
    await NotificationChannelService().create(
        name="钉钉",
        kind="dingtalk",
        target="https://oapi.example.com/robot/send?access_token=SECRET",
        secret_ref=ref.value,
    )

    checks = await doctor_service._check_notification_channels()

    assert _status_of(checks, "notification_channels") == doctor_service.PASS


@pytest.mark.asyncio
async def test_alert_webhook_check_is_satisfied_by_notification_channels(
    doctor_db: Path,
) -> None:
    """配了通知渠道就不该再提示"没配告警",否则运营会去追一个不存在的问题。"""
    without = await doctor_service._check_automation_config()
    assert _status_of(without, "alert_webhook") == doctor_service.WARN

    await NotificationChannelService().create(
        name="运维群", kind="webhook", target="https://hooks.example.com/services/AAA"
    )

    with_channel = await doctor_service._check_automation_config()

    assert _status_of(with_channel, "alert_webhook") == doctor_service.PASS
