"""`xianyu-agent service ...` Windows 任务计划管理。"""

from __future__ import annotations

import asyncio
import time

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import daemon as daemon_domain
from xianyu_agent.services.daemon_health import observe_daemon
from xianyu_agent.services.windows_service import (
    TASK_NAME,
    WATCHDOG_TASK_NAME,
    WindowsServiceError,
    end_task,
    end_watchdog_task,
    format_task_result,
    install_task,
    is_service_paused,
    query_task_status,
    set_service_paused,
    start_task,
    uninstall_task,
)

app = typer.Typer(help="Windows 任务计划:安装 / 启停 / 状态 / 卸载。")
console = Console()


def _service_error(exc: WindowsServiceError) -> typer.Exit:
    console.print(f"[red]{exc}[/red]")
    return typer.Exit(code=1)


@app.command("install")
def install(
    startup: str = typer.Option(
        "user",
        "--startup",
        help="user=当前用户登录后启动;system=开机启动(需管理员权限)。",
    ),
    start_now: bool = typer.Option(
        False,
        "--start-now",
        help="安装后立即启动任务。",
    ),
) -> None:
    """安装或更新 Windows 任务计划。"""
    if startup not in {"user", "system"}:
        console.print("[red]--startup 只支持 user 或 system。[/red]")
        raise typer.Exit(code=2)
    previous_paused = is_service_paused()
    if not start_now:
        set_service_paused(True)
    try:
        paths = install_task(startup=startup)
        if start_now:
            set_service_paused(False)
            start_task()
    except WindowsServiceError as exc:
        set_service_paused(previous_paused)
        raise _service_error(exc) from exc
    console.print(
        f"[green]OK[/green] 已安装任务 [cyan]{TASK_NAME}[/cyan] startup={startup}。"
    )
    console.print(f"解释器:{paths.pythonw_executable}")
    console.print(f"工作目录:{paths.project_root}")
    console.print(f"任务 XML:{paths.xml_path}")
    console.print(f"Watchdog XML:{paths.watchdog_xml_path}")
    if not start_now:
        console.print(
            "[yellow]任务已安装并开启暂停门,Watchdog 不会新拉起 daemon;"
            "执行 service start 后恢复运行。[/yellow]"
        )


@app.command("start")
def start(
    wait: float = typer.Option(20.0, "--wait", min=0.0, help="等待 daemon online 秒数。"),
) -> None:
    """立即运行已安装任务并等待 daemon 上线。"""
    try:
        set_service_paused(False)
        start_task()
    except WindowsServiceError as exc:
        raise _service_error(exc) from exc
    if wait > 0 and not asyncio.run(_wait_daemon(online=True, timeout_s=wait)):
        console.print(f"[red]任务已触发,但 daemon 在 {wait:.1f}s 内未上线。[/red]")
        raise typer.Exit(code=2)
    console.print(f"[green]OK[/green] 已启动任务 {TASK_NAME}。")


@app.command("stop")
def stop(
    wait: float = typer.Option(20.0, "--wait", min=0.0, help="等待 daemon 退出秒数。"),
) -> None:
    """先请求 daemon 优雅退出,超时后结束任务。"""

    async def _request() -> int:
        return await daemon_domain.request_shutdown()

    set_service_paused(True)
    requested = asyncio.run(_request())
    stopped = wait <= 0 or asyncio.run(_wait_daemon(online=False, timeout_s=wait))
    try:
        watchdog = query_task_status(WATCHDOG_TASK_NAME)
        if watchdog.installed and watchdog.state.casefold() == "running":
            end_watchdog_task()
        if not stopped or requested == 0:
            task = query_task_status()
            if task.installed and task.state.casefold() == "running":
                end_task()
    except WindowsServiceError as exc:
        status = query_task_status()
        if status.state.casefold() == "running":
            raise _service_error(exc) from exc
    if not stopped:
        console.print("[yellow]daemon 未在等待期内退出,已结束任务实例。[/yellow]")
    else:
        console.print(f"[green]OK[/green] 已停止任务 {TASK_NAME}。")


@app.command("status")
def status() -> None:
    """查看任务计划与 daemon 运行状态。"""
    try:
        task = query_task_status()
        watchdog = query_task_status(WATCHDOG_TASK_NAME)
    except WindowsServiceError as exc:
        raise _service_error(exc) from exc
    row = asyncio.run(daemon_domain.latest_instance())
    health = observe_daemon(row)
    daemon_state = health.observed
    daemon_pid = "-"
    if row is not None:
        daemon_pid = (
            f"{row.pid} "
            f"({'alive' if health.process_alive else 'dead' if health.active else 'historical'})"
        )
    table = Table(title="Windows 常驻任务")
    table.add_column("task")
    table.add_column("installed")
    table.add_column("task_state")
    table.add_column("watchdog")
    table.add_column("task_result")
    table.add_column("watchdog_result")
    table.add_column("daemon")
    table.add_column("pid")
    table.add_row(
        TASK_NAME,
        "Y" if task.installed else "N",
        task.state,
        watchdog.state,
        format_task_result(task.last_task_result),
        format_task_result(watchdog.last_task_result),
        daemon_state,
        daemon_pid,
    )
    console.print(table)


@app.command("uninstall")
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """停止 daemon 并删除 Windows 任务计划。"""
    if not yes and not typer.confirm(f"确认删除任务 {TASK_NAME}?"):
        raise typer.Abort()
    set_service_paused(True)
    asyncio.run(daemon_domain.request_shutdown())
    stopped = asyncio.run(_wait_daemon(online=False, timeout_s=10.0))
    try:
        watchdog = query_task_status(WATCHDOG_TASK_NAME)
        if watchdog.installed and watchdog.state.casefold() == "running":
            end_watchdog_task()
        task = query_task_status()
        if not stopped and task.installed and task.state.casefold() == "running":
            end_task()
        uninstall_task()
    except WindowsServiceError as exc:
        raise _service_error(exc) from exc
    console.print(f"[green]OK[/green] 已卸载任务 {TASK_NAME}。")


async def _wait_daemon(*, online: bool, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = await daemon_domain.latest_instance()
        health = observe_daemon(row)
        reached = health.healthy if online else not health.process_alive
        if reached:
            return True
        await asyncio.sleep(0.5)
    return False
