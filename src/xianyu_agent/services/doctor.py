"""P0.4 本地健康检查,只读取状态并输出脱敏结果。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import InvalidToken
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from xianyu_agent.application.ports.secret_vault import SecretRef
from xianyu_agent.config import get_settings
from xianyu_agent.db import engine_info, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, daemon as daemon_domain
from xianyu_agent.infrastructure.database.models import (
    Account as AccountRow,
    DeliveryPolicy as DeliveryPolicyRow,
    DeliveryTemplateRow,
    InventoryPool as InventoryPoolRow,
    PlatformAutomationConfig as PlatformAutomationConfigRow,
)
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyNotificationChannelRepository,
)
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault
from xianyu_agent.services.daemon_health import observe_daemon
from xianyu_agent.services.observability import build_runtime_snapshot
from xianyu_agent.services.windows_service import (
    TASK_NAME,
    WATCHDOG_TASK_NAME,
    WindowsServiceError,
    format_task_result,
    is_service_paused,
    query_task_status,
)

PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    summary: str
    detail: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DiagnosticCheck, ...]

    @property
    def exit_code(self) -> int:
        return 1 if any(check.status == FAIL for check in self.checks) else 0

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(check.status == status for check in self.checks)
            for status in (PASS, WARN, FAIL)
        }


async def run_doctor(*, startup: bool = False) -> DoctorReport:
    """运行完整检查;单项失败不会阻止其余检查。"""
    checks: list[DiagnosticCheck] = []
    for runner in (
        _check_database,
        _check_migrations,
        _check_fernet_key,
        _check_account_cookies,
        _check_log_directory,
        _check_daemon_instances,
        _check_automation_config,
        _check_notification_channels,
        _check_delivery_config,
        _check_api_binding,
    ):
        try:
            checks.extend(await runner())
        except Exception as exc:
            name = runner.__name__.removeprefix("_check_").replace("_", " ")
            checks.append(DiagnosticCheck(name, FAIL, f"检查异常:{type(exc).__name__}"))
    try:
        checks.extend(await _check_worker_alignment(startup=startup))
    except Exception as exc:
        checks.append(DiagnosticCheck("worker alignment", FAIL, f"检查异常:{type(exc).__name__}"))
    checks.extend(_check_windows_tasks())
    return DoctorReport(tuple(checks))


async def _check_database() -> list[DiagnosticCheck]:
    settings = get_settings()
    info = engine_info()
    async with get_async_session() as session:
        result = await session.execute(text("SELECT 1"))
        if result.scalar_one() != 1:  # pragma: no cover - SQLite invariant
            return [DiagnosticCheck("database", FAIL, "SELECT 1 返回异常")]
    if info["backend"] != "sqlite":
        # 非 SQLite 后端没有本地文件可判可写:连接探针通过即放行,权限问题会在迁移或首次
        # 写入时以明确错误暴露。URL 只输出脱敏形式(engine_info 已隐藏密码)。
        return [DiagnosticCheck("database", PASS, f"连接正常({info['backend']})", str(info["url"]))]
    path = _sqlite_file_path(settings.db_url)
    if path is None:
        # 内存库没有文件可判可写,连接探针通过即可。
        return [DiagnosticCheck("database", PASS, "SQLite 内存库连接正常", str(info["url"]))]
    writable = path.exists() and os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)
    if writable:
        try:
            connection = sqlite3.connect(path, timeout=3.0)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            finally:
                connection.close()
        except sqlite3.Error:
            writable = False
    status = PASS if writable else FAIL
    summary = "连接正常且数据库/目录可写" if writable else "数据库或其目录不可写"
    return [DiagnosticCheck("database", status, summary, str(path.resolve()))]


async def _check_migrations() -> list[DiagnosticCheck]:
    async with get_async_session() as session:
        current = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one()
    config = Config(str(_project_root() / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_project_root() / "src/xianyu_agent/db/migrations")
    )
    # Alembic 1.19 起,未显式指定 path_separator 会发 DeprecationWarning;在 -W error 的测试环境
    # 里那会被当成检查异常,所以在 doctor 里也显式声明一次。
    config.set_main_option("path_separator", "os")
    heads = tuple(ScriptDirectory.from_config(config).get_heads())
    if len(heads) != 1:
        return [DiagnosticCheck("migrations", FAIL, f"迁移 head 数异常:{len(heads)}")]
    expected = heads[0]
    status = PASS if current == expected else FAIL
    summary = "数据库迁移为最新" if status == PASS else "数据库迁移不是最新"
    return [DiagnosticCheck("migrations", status, summary, f"current={current}, head={expected}")]


async def _check_fernet_key() -> list[DiagnosticCheck]:
    settings = get_settings()
    try:
        _ = settings.fernet
    except (ValueError, TypeError):
        return [DiagnosticCheck("fernet_key", FAIL, "FERNET_KEY 缺失或格式无效")]
    return [DiagnosticCheck("fernet_key", PASS, "FERNET_KEY 格式有效")]


async def _check_account_cookies() -> list[DiagnosticCheck]:
    from sqlalchemy import select  # noqa: PLC0415

    from xianyu_agent.db import Cookie, get_async_session  # noqa: PLC0415

    settings = get_settings()
    accounts = await domain_accounts.list_accounts()
    async with get_async_session() as session:
        rows = list((await session.execute(select(Cookie))).scalars().all())
    by_account: dict[int, list[Cookie]] = {}
    for row in rows:
        by_account.setdefault(row.account_id, []).append(row)
    checks: list[DiagnosticCheck] = []
    for account in accounts:
        cookies = by_account.get(account.id, [])
        if not cookies:
            status = FAIL if account.enabled else WARN
            checks.append(
                DiagnosticCheck(
                    f"cookie:{account.account_id}",
                    status,
                    "启用账号缺少 Cookie" if account.enabled else "停用账号没有 Cookie",
                )
            )
            continue
        decryptable = False
        for cookie in cookies:
            try:
                plaintext = settings.fernet.decrypt(cookie.encrypted_value.encode("utf-8"))
            except (InvalidToken, ValueError):
                continue
            if plaintext:
                decryptable = True
                break
        status = PASS if decryptable else FAIL if account.enabled else WARN
        if decryptable:
            summary = "加密 Cookie 可解密"
        elif account.enabled:
            summary = "启用账号 Cookie 无法用当前密钥解密"
        else:
            summary = "停用账号保留了不可解密的旧 Cookie"
        checks.append(
            DiagnosticCheck(
                f"cookie:{account.account_id}",
                status,
                summary,
            )
        )
    if not accounts:
        checks.append(DiagnosticCheck("cookies", WARN, "尚未添加账号"))
    return checks


async def _check_log_directory() -> list[DiagnosticCheck]:
    settings = get_settings()
    path = settings.log_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="doctor-", dir=path, delete=True):
            pass
    except OSError as exc:
        return [DiagnosticCheck("log_directory", FAIL, "日志目录不可写", type(exc).__name__)]
    return [DiagnosticCheck("log_directory", PASS, "日志目录可写", str(path.resolve()))]


async def _check_daemon_instances() -> list[DiagnosticCheck]:
    rows = await daemon_domain.active_instances()
    observed = [(row, observe_daemon(row, check_process=False)) for row in rows]
    healthy = [(row, health) for row, health in observed if health.healthy]
    unhealthy = [(row, health) for row, health in observed if not health.healthy]
    if healthy:
        healthy_ids = ",".join(row.instance_id[:8] for row, _health in healthy)
        if unhealthy:
            stale_rows = ",".join(
                f"{row.instance_id[:8]}:{health.observed}" for row, health in unhealthy
            )
            return [
                DiagnosticCheck(
                    "daemon_instances",
                    WARN,
                    f"{len(healthy)} 个 daemon 健康,存在遗留活跃记录",
                    f"healthy={healthy_ids}; stale={stale_rows}",
                )
            ]
        return [
            DiagnosticCheck(
                "daemon_instances",
                PASS,
                f"{len(healthy)} 个 daemon 实例健康",
                healthy_ids,
            )
        ]
    if rows:
        states = ",".join(f"{row.instance_id[:8]}:{health.observed}" for row, health in observed)
        return [
            DiagnosticCheck(
                "daemon_instances",
                WARN,
                "没有健康 daemon,存在待启动实例清理的过期记录",
                states,
            )
        ]
    return [DiagnosticCheck("daemon_instances", WARN, "当前没有活跃 daemon")]


async def _check_automation_config() -> list[DiagnosticCheck]:
    """检查会自动生效的配置是否真的能生效。

    无人值守最怕"看起来配好了、实际静默不干活":启用了但没校准的平台动作、指向停用池或缺失模板的
    发货策略、范围指向不存在账号的库存池,都属于这一类。这里只读检查,不改任何配置。
    """
    checks: list[DiagnosticCheck] = []
    async with get_async_session() as session:
        uncalibrated = list(
            (
                await session.scalars(
                    select(PlatformAutomationConfigRow).where(
                        PlatformAutomationConfigRow.enabled.is_(True),
                        PlatformAutomationConfigRow.calibration_approved_at.is_(None),
                    )
                )
            ).all()
        )
    if uncalibrated:
        detail = ",".join(f"#{row.id}:{row.action}" for row in uncalibrated[:5])
        checks.append(
            DiagnosticCheck(
                "platform_automation",
                FAIL,
                f"{len(uncalibrated)} 个已启用动作缺少真实校准批准",
                detail,
            )
        )
    else:
        checks.append(
            DiagnosticCheck("platform_automation", PASS, "已启用动作都有校准批准", None)
        )

    settings = get_settings()
    webhook = settings.alert_webhook_url.strip()
    if not webhook:
        # 配了通知渠道就不该再提示"没配告警":那条提示会让运营去追一个不存在的问题。
        channels = await _enabled_channel_count()
        if channels:
            checks.append(
                DiagnosticCheck(
                    "alert_webhook",
                    PASS,
                    f"未用环境变量告警地址;已有 {channels} 条启用的通知渠道",
                )
            )
        else:
            checks.append(DiagnosticCheck("alert_webhook", WARN, "未配置外部告警(默认关闭)"))
    elif not webhook.lower().startswith("https://"):
        # 不要把地址本身写进输出:路径里可能带令牌。
        checks.append(DiagnosticCheck("alert_webhook", WARN, "告警地址不是 https,daemon 会拒发"))
    else:
        checks.append(DiagnosticCheck("alert_webhook", PASS, "告警地址已配置(https)"))
    return checks


async def _enabled_channel_count() -> int:
    async with get_async_session() as session:
        channels = await SqlAlchemyNotificationChannelRepository(session).list_channels(
            only_enabled=True
        )
    return len(channels)


async def _check_notification_channels() -> list[DiagnosticCheck]:
    """检查通知渠道:没配渠道不会报错,但配了却发不出去要提前说。"""
    async with get_async_session() as session:
        channels = await SqlAlchemyNotificationChannelRepository(session).list_channels(
            only_enabled=True
        )
    if not channels:
        return [DiagnosticCheck("notification_channels", WARN, "未配置通知渠道(默认不外发)")]

    vault = SqlAlchemySecretVault(FernetKeyring.from_environment())
    # 只报渠道名与引用名:引用名不是密钥本身,但也足够让运营定位到该补哪一个。
    missing_secrets = [
        f"{channel.name}:{channel.secret_ref}"
        for channel in channels
        if channel.secret_ref and not await vault.configured(SecretRef(channel.secret_ref))
    ]
    if missing_secrets:
        return [
            DiagnosticCheck(
                "notification_channels",
                FAIL,
                f"{len(missing_secrets)} 个启用渠道的密钥引用在 SecretVault 里不存在",
                ",".join(missing_secrets[:5]),
            )
        ]
    return [DiagnosticCheck("notification_channels", PASS, f"{len(channels)} 个启用渠道配置完整")]


async def _check_api_binding() -> list[DiagnosticCheck]:
    """Agent HTTP API 是文档化的无鉴权边界;绑定到非本机地址时提醒运营。

    健康服务器只暴露 `/health`,所以 `XIANYU_HTTP_HOST=0.0.0.0` 本身没问题;有问题的只有
    `mcp serve` 绑定的 `XIANYU_MCP_HOST`——那后面是 95 个可写端点。
    """
    settings = get_settings()
    host = settings.mcp_host.strip().lower()
    loopback = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if host in loopback:
        return [DiagnosticCheck("api_binding", PASS, f"Agent API 仅本机可达({host})")]
    return [
        DiagnosticCheck(
            "api_binding",
            WARN,
            "Agent API 绑定到非本机地址,而它没有鉴权(见 docs/api-reference.md)",
            host or "-",
        )
    ]


async def _check_delivery_config() -> list[DiagnosticCheck]:
    """启用的发货策略必须真的能解析到池与模板,否则自动发货会静默失效。"""
    checks: list[DiagnosticCheck] = []
    async with get_async_session() as session:
        policies = list(
            (
                await session.scalars(
                    select(DeliveryPolicyRow).where(DeliveryPolicyRow.enabled.is_(True))
                )
            ).all()
        )
        pools = {row.id: row for row in (await session.scalars(select(InventoryPoolRow))).all()}
        templates = {
            row.name: row for row in (await session.scalars(select(DeliveryTemplateRow))).all()
        }
        account_ids = set((await session.scalars(select(AccountRow.account_id))).all())

    broken_policies: list[str] = []
    disabled_pools: list[str] = []
    missing_templates: list[str] = []
    disabled_templates: list[str] = []
    for policy in policies:
        pool = pools.get(policy.inventory_pool_id)
        if pool is None:
            broken_policies.append(f"#{policy.id}:missing-pool")
        elif not pool.enabled:
            disabled_pools.append(f"#{policy.id}->pool#{pool.id}")
        if policy.template_ref:
            template = templates.get(policy.template_ref)
            if template is None:
                missing_templates.append(f"#{policy.id}->{policy.template_ref}")
            elif not template.enabled:
                disabled_templates.append(f"#{policy.id}->{policy.template_ref}")
    if broken_policies:
        checks.append(
            DiagnosticCheck(
                "delivery_policies",
                FAIL,
                f"{len(broken_policies)} 条启用策略指向不存在的库存池",
                ",".join(broken_policies[:5]),
            )
        )
    if disabled_pools:
        checks.append(
            DiagnosticCheck(
                "delivery_policies",
                FAIL,
                f"{len(disabled_pools)} 条启用策略指向已停用的库存池",
                ",".join(disabled_pools[:5]),
            )
        )
    if missing_templates or disabled_templates:
        checks.append(
            DiagnosticCheck(
                "delivery_templates",
                FAIL,
                (
                    f"{len(missing_templates)} 条策略引用的模板不存在、"
                    f"{len(disabled_templates)} 条引用已停用模板"
                ),
                ",".join((missing_templates + disabled_templates)[:5]),
            )
        )
    elif policies:
        checks.append(
            DiagnosticCheck("delivery_policies", PASS, f"{len(policies)} 条启用策略可解析", None)
        )
    else:
        checks.append(
            DiagnosticCheck("delivery_policies", WARN, "没有启用的发货策略(将回落旧卡密链路)")
        )

    orphan_scopes = [
        f"pool#{row.id}->{row.account_scope}"
        for row in pools.values()
        if row.account_scope and row.account_scope not in account_ids
    ]
    if orphan_scopes:
        checks.append(
            DiagnosticCheck(
                "pool_scope",
                WARN,
                f"{len(orphan_scopes)} 个库存池限定的账号不存在(该池永远匹配不上)",
                ",".join(orphan_scopes[:5]),
            )
        )

    return checks


async def _check_worker_alignment(*, startup: bool = False) -> list[DiagnosticCheck]:
    snapshot = await build_runtime_snapshot()
    drifted = snapshot.drifted_accounts
    if not drifted:
        return [
            DiagnosticCheck(
                "worker_alignment",
                PASS,
                f"{len(snapshot.accounts)} 个账号期望/实际状态一致",
            )
        ]
    if (
        startup
        and not snapshot.healthy_daemons
        and all(
            account.enabled
            and account.desired_state == "running"
            and account.actual_state in {"disabled", "offline", "disconnected"}
            for account in drifted
        )
    ):
        detail = "; ".join(f"{row.account_id}:{row.issue}" for row in drifted)
        return [
            DiagnosticCheck(
                "worker_alignment",
                WARN,
                f"{len(drifted)} 个账号等待 daemon 启动后恢复",
                detail,
            )
        ]
    detail = "; ".join(f"{row.account_id}:{row.issue}" for row in drifted)
    return [
        DiagnosticCheck(
            "worker_alignment",
            FAIL,
            f"{len(drifted)} 个账号状态漂移",
            detail,
        )
    ]


def _check_windows_tasks() -> list[DiagnosticCheck]:
    if os.name != "nt":
        return [DiagnosticCheck("windows_tasks", WARN, "非 Windows,跳过任务计划检查")]
    try:
        task = query_task_status(TASK_NAME)
        watchdog = query_task_status(WATCHDOG_TASK_NAME)
    except WindowsServiceError as exc:
        return [DiagnosticCheck("windows_tasks", FAIL, "任务计划状态读取失败", str(exc))]
    if not task.installed or not watchdog.installed:
        return [DiagnosticCheck("windows_tasks", FAIL, "daemon 或 Watchdog 任务未安装")]
    paused = is_service_paused()
    if paused:
        return [DiagnosticCheck("windows_tasks", WARN, "任务已安装,服务处于人工暂停")]
    status = PASS if watchdog.last_task_result in {0, 0x00041301} else FAIL
    summary = "两个任务已安装,Watchdog 最近执行成功" if status == PASS else "Watchdog 最近执行失败"
    detail = (
        f"daemon={task.state}/{format_task_result(task.last_task_result)}, "
        f"watchdog={watchdog.state}/{format_task_result(watchdog.last_task_result)}"
    )
    return [DiagnosticCheck("windows_tasks", status, summary, detail)]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _sqlite_file_path(db_url: str) -> Path | None:
    """从实际生效的 URL 取 SQLite 文件路径;非文件库(内存/远程)返回 None。

    不能用 resolved_db_path 代替:显式 XIANYU_DATABASE_URL 指向别的 sqlite 文件时,
    resolved_db_path 仍是 data_dir/xianyu.db,会得出「目录不可写」的错误结论。
    """
    url = make_url(db_url)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database)
