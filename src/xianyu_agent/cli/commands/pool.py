"""`xianyu-agent pool ...` 账号级跨进程 Worker 控制。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import daemon as daemon_domain, worker_commands
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.services.daemon_health import observe_daemon
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="账号级 Worker 池(通过常驻 daemon 跨进程启停)。")
console = Console()


def _status_table(rows: list[dict]) -> Table:
    table = Table(title=f"账号池状态 ({len(rows)})  {format_local(datetime.now(UTC))}")
    table.add_column("account_id", style="cyan")
    table.add_column("enabled")
    table.add_column("desired")
    table.add_column("db", style="magenta")
    table.add_column("reconnects")
    table.add_column("last_heartbeat")
    table.add_column("risk", overflow="fold")
    table.add_column("last_error", overflow="fold")
    for row in rows:
        table.add_row(
            row["account_id"],
            "Y" if row["enabled"] else "N",
            row["desired_state"],
            row["db_status"],
            str(row["reconnect_attempts"]),
            row["last_heartbeat_at"] or "-",
            _risk_text(row),
            (row["last_error"] or "-")[:80],
        )
    return table


def _risk_text(row: dict) -> str:
    code = row.get("risk_code")
    if not code:
        return "-"
    if row.get("risk_recovery_required"):
        return f"{code} / {row.get('risk_cooldown_until') or '需手动刷新'}"
    return str(code)


@app.command("status")
def status() -> None:
    """查看所有账号的期望状态与持久化 Worker 实际状态。"""

    async def _run() -> None:
        rows = await AccountPool().status()
        if not rows:
            console.print("[dim]暂无账号。先 `auth login` 或 `account add`。[/dim]")
            return
        console.print(_status_table(rows))

    asyncio.run(_run())


@app.command("start")
def start_one(
    account_id: str = typer.Option(..., "--account", "-a"),
    wait: float = typer.Option(15.0, "--wait", min=0.0, help="等待 daemon 回执秒数;0=不等。"),
) -> None:
    """请求 daemon 启动一个账号 Worker。"""
    _run_one(account_id, "start", wait=wait)


@app.command("stop")
def stop_one(
    account_id: str = typer.Option(..., "--account", "-a"),
    wait: float = typer.Option(15.0, "--wait", min=0.0, help="等待 daemon 回执秒数;0=不等。"),
) -> None:
    """请求 daemon 停止一个账号 Worker。"""
    _run_one(account_id, "stop", wait=wait)


@app.command("restart")
def restart_one(
    account_id: str = typer.Option(..., "--account", "-a"),
    wait: float = typer.Option(15.0, "--wait", min=0.0, help="等待 daemon 回执秒数;0=不等。"),
) -> None:
    """请求 daemon 重建一个账号 Worker。"""
    _run_one(account_id, "restart", wait=wait)


@app.command("start-all")
def start_all(
    wait: float = typer.Option(15.0, "--wait", min=0.0, help="等待 daemon 回执秒数;0=不等。"),
) -> None:
    """请求 daemon 启动全部启用账号。"""
    _run_many("start", wait=wait)


@app.command("stop-all")
def stop_all(
    wait: float = typer.Option(15.0, "--wait", min=0.0, help="等待 daemon 回执秒数;0=不等。"),
) -> None:
    """请求 daemon 停止全部启用账号。"""
    _run_many("stop", wait=wait)


def _run_one(account_id: str, action: str, *, wait: float) -> None:
    async def _run() -> None:
        try:
            command = await worker_commands.submit(account_id, action)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if command is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        await _report_commands([command.command_id], wait=wait)

    asyncio.run(_run())


def _run_many(action: str, *, wait: float) -> None:
    async def _run() -> None:
        commands = await worker_commands.submit_many(action)
        if not commands:
            console.print("[yellow]没有启用账号。[/yellow]")
            return
        await _report_commands([command.command_id for command in commands], wait=wait)

    asyncio.run(_run())


async def _report_commands(command_ids: list[str], *, wait: float) -> None:
    if wait <= 0:
        console.print(
            f"[yellow]已提交 {len(command_ids)} 条命令,未等待执行。[/yellow] "
            f"command={','.join(command_id[:8] for command_id in command_ids)}"
        )
        return
    if not await _daemon_is_fresh():
        console.print(
            "[red]daemon 不在线或心跳已过期,命令已保留为 pending,当前未执行。[/red]"
        )
        raise typer.Exit(code=2)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        rows = [await worker_commands.get(command_id) for command_id in command_ids]
        if all(
            row is not None and row.status in worker_commands.TERMINAL_STATUSES for row in rows
        ):
            failures = [row for row in rows if row is not None and row.status == "failed"]
            if failures:
                detail = "; ".join(
                    f"{row.command_id[:8]}:{row.error or 'unknown error'}" for row in failures
                )
                console.print(f"[red]命令执行失败:{detail}[/red]")
                raise typer.Exit(code=1)
            console.print(
                f"[green]OK[/green] daemon 已执行 {len(command_ids)} 条账号命令:"
                + ",".join(command_id[:8] for command_id in command_ids)
            )
            return
        await asyncio.sleep(0.2)
    console.print(
        f"[red]等待 daemon 回执超时({wait:.1f}s),命令状态未确认;不要按成功处理。[/red]"
    )
    raise typer.Exit(code=2)


async def _daemon_is_fresh() -> bool:
    row = await daemon_domain.latest_instance()
    return observe_daemon(row).healthy
