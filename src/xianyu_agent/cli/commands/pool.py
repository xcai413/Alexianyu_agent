"""`xianyu-agent pool ...` subcommands: multi-account worker pool."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.services.heartbeat import purge_old_messages
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="多账号 Worker 池(启停 / 状态)。")
console = Console()


def _status_table(rows: list[dict]) -> Table:
    t = Table(title=f"账号池状态 ({len(rows)})  {format_local(datetime.now(UTC))}")
    t.add_column("account_id", style="cyan")
    t.add_column("enabled")
    t.add_column("worker", style="magenta")
    t.add_column("db", style="magenta")
    t.add_column("reconnects")
    t.add_column("last_heartbeat")
    t.add_column("last_error", overflow="fold")
    for r in rows:
        t.add_row(
            r["account_id"],
            "Y" if r["enabled"] else "N",
            r["worker_state"],
            r["db_status"],
            str(r["reconnect_attempts"]),
            (r["last_heartbeat_at"] or "-")[:19],
            (r["last_error"] or "-")[:50],
        )
    return t


@app.command("status")
def status() -> None:
    """查看所有账号的 worker 状态(从 DB 读心跳,任意进程可用)。"""

    async def _run() -> None:
        rows = await AccountPool().status()
        if not rows:
            console.print("[dim]暂无账号。先 `auth login` 或 `account add`。[/dim]")
            return
        console.print(_status_table(rows))

    asyncio.run(_run())


@app.command("start-all")
def start_all(
    seconds: float = typer.Option(60.0, "--seconds", "-s", help="前台运行时长(秒)。"),
    refresh: float = typer.Option(2.0, "--refresh", "-r", help="刷新间隔(秒)。"),
    purge_interval: float = typer.Option(
        3600.0, "--purge-interval", help="消息清理间隔(秒);0=禁用。"
    ),
) -> None:
    """前台启动所有启用账号的 Worker,实时显示状态,Ctrl+C 停止。"""
    _run_foreground(
        account_id=None, seconds=seconds, refresh=refresh, purge_interval=purge_interval
    )


@app.command("start")
def start_one(
    account_id: str = typer.Option(..., "--account", "-a"),
    seconds: float = typer.Option(60.0, "--seconds", "-s", help="前台运行时长(秒)。"),
    refresh: float = typer.Option(2.0, "--refresh", "-r", help="刷新间隔(秒)。"),
    purge_interval: float = typer.Option(
        3600.0, "--purge-interval", help="消息清理间隔(秒);0=禁用。"
    ),
) -> None:
    """前台启动单个账号 Worker。"""
    _run_foreground(
        account_id=account_id, seconds=seconds, refresh=refresh, purge_interval=purge_interval
    )


@app.command("stop")
def stop_one(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """标记 Worker 离线(DB 层)。

    说明:池进程是独立前台进程,跨进程无法直接停止;此命令把 worker_status
    置为 offline。若另一终端正在运行 start-all,请到该终端按 Ctrl+C。
    """

    async def _run() -> None:
        await domain_accounts.mark_worker_offline(account_id, reason="CLI stop")
        console.print(f"[yellow]OK[/yellow] {account_id} 已标记离线。")

    asyncio.run(_run())


@app.command("stop-all")
def stop_all_mark() -> None:
    """标记所有账号 Worker 离线(DB 层)。"""

    async def _run() -> None:
        accounts = await domain_accounts.list_accounts()
        for acc in accounts:
            await domain_accounts.mark_worker_offline(acc.account_id, reason="CLI stop-all")
        console.print(f"[yellow]OK[/yellow] 已标记 {len(accounts)} 个账号离线。")

    asyncio.run(_run())


def _run_foreground(
    account_id: str | None, *, seconds: float, refresh: float, purge_interval: float
) -> None:
    async def _run() -> None:
        pool = await AccountPool.from_enabled_accounts()
        if account_id is not None:
            if not pool.has(account_id):
                console.print(f"[red]账号 {account_id} 不存在或未启用。[/red]")
                raise typer.Exit(code=1)
            pool.start(account_id)
        else:
            started = pool.start_all()
            console.print(
                f"已启动 [cyan]{len(started)}[/cyan] 个 Worker: {', '.join(started) or '(无)'}"
            )
        stop_at = time.monotonic() + seconds
        last_purge = time.monotonic()
        stop_event = asyncio.Event()

        async def _tick() -> Table:
            rows = await pool.status()
            return _status_table(rows)

        try:
            with Live(await _tick(), refresh_per_second=1.0 / refresh, console=console) as live:
                while not stop_event.is_set() and time.monotonic() < stop_at:
                    with __import__("contextlib").suppress(TimeoutError):
                        await asyncio.wait_for(stop_event.wait(), timeout=refresh)
                        break
                    live.update(await _tick())
                    if purge_interval > 0 and time.monotonic() - last_purge >= purge_interval:
                        deleted = await purge_old_messages()
                        if deleted:
                            console.print(f"[dim]已清理 {deleted} 条过期消息[/dim]")
                        last_purge = time.monotonic()
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            await pool.stop_all()
            console.print("\n[dim]已停止全部 Worker。[/dim]")

    asyncio.run(_run())
