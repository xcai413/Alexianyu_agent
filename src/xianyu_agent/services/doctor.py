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
from sqlalchemy import text

from xianyu_agent.config import get_settings
from xianyu_agent.db import get_async_session
from xianyu_agent.domain import accounts as domain_accounts, daemon as daemon_domain
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


async def run_doctor() -> DoctorReport:
    """运行完整检查;单项失败不会阻止其余检查。"""
    checks: list[DiagnosticCheck] = []
    for runner in (
        _check_database,
        _check_migrations,
        _check_fernet_key,
        _check_account_cookies,
        _check_log_directory,
        _check_daemon_instances,
        _check_worker_alignment,
    ):
        try:
            checks.extend(await runner())
        except Exception as exc:
            name = runner.__name__.removeprefix("_check_").replace("_", " ")
            checks.append(DiagnosticCheck(name, FAIL, f"检查异常:{type(exc).__name__}"))
    checks.extend(_check_windows_tasks())
    return DoctorReport(tuple(checks))


async def _check_database() -> list[DiagnosticCheck]:
    settings = get_settings()
    path = settings.resolved_db_path
    async with get_async_session() as session:
        result = await session.execute(text("SELECT 1"))
        if result.scalar_one() != 1:  # pragma: no cover - SQLite invariant
            return [DiagnosticCheck("database", FAIL, "SELECT 1 返回异常")]
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
        current = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    config = Config(str(_project_root() / "alembic.ini"))
    config.set_main_option("script_location", str(_project_root() / "src/xianyu_agent/db/migrations"))
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
    healthy = [(row, observe_daemon(row)) for row in rows]
    live = [(row, health) for row, health in healthy if health.process_alive]
    if len(live) > 1:
        return [
            DiagnosticCheck(
                "daemon_instances",
                FAIL,
                f"检测到 {len(live)} 个存活 daemon 实例",
                ",".join(str(row.pid) for row, _health in live),
            )
        ]
    if len(live) == 1 and live[0][1].healthy:
        row, _health = live[0]
        stale_rows = [
            f"{candidate.pid}:{health.observed}"
            for candidate, health in healthy
            if candidate.instance_id != row.instance_id
        ]
        if stale_rows:
            return [
                DiagnosticCheck(
                    "daemon_instances",
                    WARN,
                    "单实例 daemon 健康,但存在遗留活跃记录",
                    ",".join(stale_rows),
                )
            ]
        return [DiagnosticCheck("daemon_instances", PASS, "单实例 daemon 健康", f"pid={row.pid}")]
    if rows:
        states = ",".join(f"{row.pid}:{health.observed}" for row, health in healthy)
        return [DiagnosticCheck("daemon_instances", FAIL, "活跃记录没有健康 daemon", states)]
    return [DiagnosticCheck("daemon_instances", WARN, "当前没有活跃 daemon")]


async def _check_worker_alignment() -> list[DiagnosticCheck]:
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
