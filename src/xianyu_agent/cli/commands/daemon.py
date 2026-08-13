"""`xianyu-agent daemon ...` 常驻运行命令。"""

from __future__ import annotations

import asyncio
import ctypes
import os
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.config import get_settings
from xianyu_agent.domain import daemon as daemon_domain
from xianyu_agent.services.daemon_lock import DaemonAlreadyRunningError
from xianyu_agent.services.logging_setup import redact_text
from xianyu_agent.services.runtime_daemon import run_runtime_daemon
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="常驻 daemon(无时限运行 / 状态 / 停止 / 日志)。")
console = Console()
STALE_AFTER_S = 90.0


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            return False
        close_handle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@app.command("run")
def run() -> None:
    """前台运行无时限 daemon;Ctrl+C 或 `daemon stop` 有序退出。"""
    settings = get_settings()
    console.print(
        "[green]启动常驻 daemon[/green],按 Ctrl+C 停止。"
        f"日志:[cyan]{settings.daemon_log_path}[/cyan]"
    )
    try:
        asyncio.run(run_runtime_daemon())
    except DaemonAlreadyRunningError as exc:
        console.print(f"[red]启动失败:{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command("status")
def status() -> None:
    """查询最近一次 daemon 的持久化状态和心跳。"""

    async def _run() -> None:
        row = await daemon_domain.latest_instance()
        if row is None:
            console.print("[dim]尚无 daemon 运行记录。[/dim]")
            return
        now = datetime.now(UTC)
        age_s = max(0.0, (now - _aware_utc(row.last_heartbeat_at)).total_seconds())
        pid_alive = _pid_alive(row.pid)
        active = row.status in daemon_domain.ACTIVE_STATUSES
        observed = "online" if active and age_s <= STALE_AFTER_S and pid_alive else row.status
        if active and age_s > STALE_AFTER_S:
            observed = "stale"
        elif active and not pid_alive:
            observed = "dead"
        table = Table(title="daemon 状态")
        table.add_column("instance_id", style="cyan")
        table.add_column("observed")
        table.add_column("db")
        table.add_column("pid")
        table.add_column("version")
        table.add_column("started_at")
        table.add_column("heartbeat")
        table.add_column("age_s")
        table.add_column("last_error", overflow="fold")
        table.add_row(
            row.instance_id[:12],
            observed,
            row.status,
            f"{row.pid} ({'alive' if pid_alive else 'dead'})",
            row.version,
            format_local(row.started_at),
            format_local(row.last_heartbeat_at),
            f"{age_s:.1f}",
            redact_text(row.last_error or "-"),
        )
        console.print(table)

    asyncio.run(_run())


@app.command("stop")
def stop() -> None:
    """通过数据库控制标记请求活跃 daemon 有序停止。"""

    async def _run() -> None:
        count = await daemon_domain.request_shutdown()
        if count == 0:
            console.print("[yellow]没有可停止的活跃 daemon。[/yellow]")
            return
        console.print(f"[green]OK[/green] 已向 {count} 个 daemon 实例提交停止请求。")

    asyncio.run(_run())


@app.command("restart")
def restart() -> None:
    """请求活跃 daemon 有序重建自身与账号池。"""

    async def _run() -> None:
        count = await daemon_domain.request_restart()
        if count == 0:
            console.print("[yellow]没有可重启的活跃 daemon;请先执行 daemon run。[/yellow]")
            return
        console.print(f"[green]OK[/green] 已向 {count} 个 daemon 实例提交重启请求。")

    asyncio.run(_run())


@app.command("logs")
def logs(
    tail: int = typer.Option(100, "--tail", min=1, max=5000, help="显示末尾行数。"),
) -> None:
    """读取 daemon 文件日志,输出前再次脱敏。"""
    path = get_settings().daemon_log_path
    if not path.exists():
        console.print(f"[dim]日志文件不存在:{path}[/dim]")
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-tail:]:
        console.print(redact_text(line), markup=False)
