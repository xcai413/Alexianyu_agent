"""`xianyu-agent protocol ...` subcommands (Phase 1)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import get_async_session
from xianyu_agent.db.models import WorkerStatus as DbWorkerStatus
from xianyu_agent.domain import (
    messages as domain_messages,
    orders as domain_orders,
)
from xianyu_agent.protocol.client import WsClient
from xianyu_agent.protocol.events import (
    MessageReceived,
    OrderCreated,
    OrderDelivered,
    OrderPaid,
    SystemNotice,
    WsFrame,
)
from xianyu_agent.services.account_worker import AccountWorker
from xianyu_agent.utils.time_utils import format_local, to_local

app = typer.Typer(help="协议层命令:连接 / 测试 / 录制回放。")
console = Console()
logger = logging.getLogger(__name__)


@app.command("connect")
def connect(
    account_id: str = typer.Option(..., "--account", "-a"),
    seconds: float = typer.Option(60.0, "--seconds", "-s", help="运行时长(秒)。"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="只打印状态,不打消息。"),
) -> None:
    """启动单账号 WS 连接 N 秒,期间打印收到的事件。"""
    if not get_settings().ws_url:
        console.print("[red]未配置 XIANYU_WS_URL[/red] .env 或留空(可配合 inject 调试)。")
    counts = {"message": 0, "order": 0, "system": 0, "error": 0}
    stop_at = time.monotonic() + seconds

    async def on_event(event) -> None:
        counts[
            "message"
            if isinstance(event, MessageReceived)
            else "order"
            if isinstance(event, (OrderCreated, OrderPaid, OrderDelivered))
            else "system"
            if isinstance(event, SystemNotice)
            else "other"
        ] = (
            counts.get(
                "message"
                if isinstance(event, MessageReceived)
                else "order"
                if isinstance(event, (OrderCreated, OrderPaid, OrderDelivered))
                else "system"
                if isinstance(event, SystemNotice)
                else "other",
                0,
            )
            + 1
        )
        if isinstance(event, MessageReceived):
            await domain_messages.upsert_inbound(event)
            if not quiet:
                console.print(
                    f"[cyan]MSG[/cyan] {event.chat_id[:16]} {event.sender_id[:12]}: {event.content[:80]}"
                )
        elif isinstance(event, (OrderCreated, OrderPaid, OrderDelivered)):
            await domain_orders.upsert_from_event(event)
            if not quiet:
                console.print(
                    f"[magenta]ORDER[/magenta] {type(event).__name__} order_id={event.order_id} status={getattr(event, 'status', '-')}"
                )
        elif isinstance(event, SystemNotice):
            if not quiet:
                console.print(f"[yellow]SYSTEM[/yellow] {event.notice_type}: {event.content[:80]}")

    async def on_state(state) -> None:
        console.print(f"[dim]STATE {state.state.value}  {state.detail or ''}[/dim]")

    async def on_error(error) -> None:
        counts["error"] += 1
        console.print(f"[red]ERROR {error.code}: {error.message}[/red]")

    async def _run() -> None:
        client = WsClient(
            account_id,
            on_event=on_event,
            on_state=on_state,
            on_error=on_error,
        )
        client.start()
        stop_event = asyncio.Event()
        try:
            while not stop_event.is_set() and time.monotonic() < stop_at:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        finally:
            stop_event.set()
            await client.stop()
        console.print(f"\n[bold]汇总:[/bold] {counts}")

    asyncio.run(_run())


@app.command("inject")
def inject(
    account_id: str = typer.Option(..., "--account", "-a"),
    fixture: str = typer.Option(..., "--fixture", "-f", help="JSONL 文件路径,每行一个 WsFrame。"),
    count: int = typer.Option(1, "--count", "-n", help="每个帧重复次数。"),
) -> None:
    """从 fixture 注入帧(用于离线测试 parser 与入库)。"""
    p = Path(fixture)
    if not p.exists():
        console.print(f"[red]fixture 不存在: {p}[/red]")
        raise typer.Exit(code=1)
    raw_frames = [
        json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not raw_frames:
        console.print("[red]fixture 为空[/red]")
        raise typer.Exit(code=1)
    console.print(f"加载 [cyan]{len(raw_frames)}[/cyan] 个帧;每个重复 {count} 次")

    async def _run() -> None:
        worker = AccountWorker(account_id)
        worker.start()
        try:
            for _ in range(count):
                for f in raw_frames:
                    worker.inject_frame(WsFrame.model_validate(f))
            await asyncio.sleep(2.0)
        finally:
            await worker.stop()

    asyncio.run(_run())
    console.print("[green]注入完成,检查 database:[/green]")
    console.print("  uv run xianyu-agent message list --account", account_id, "--since 1h")


@app.command("watch")
def watch(
    account_id: str = typer.Option(..., "--account", "-a"),
    refresh: float = typer.Option(2.0, "--refresh", help="刷新间隔(秒)。"),
) -> None:
    """实时观察账号连接状态 + 最近消息(终端 dashboard)。"""

    async def _snapshot() -> Table:
        async with get_async_session() as session:
            from xianyu_agent.db import Account as DbAccount  # noqa: PLC0415

            stmt = select(DbAccount).where(DbAccount.account_id == account_id).limit(1)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if account is None:
                return Table(title=f"{account_id}: 未配置")
            stmt_ws = select(DbWorkerStatus).where(DbWorkerStatus.account_id == account.id).limit(1)
            ws_row = (await session.execute(stmt_ws)).scalar_one_or_none()
            recent = await domain_messages.list_recent(account_id=account_id, limit=10)
        t = Table(title=f"实时: {account_id}  ({format_local(datetime.now(UTC))})")
        t.add_column("字段", style="cyan")
        t.add_column("值")
        t.add_row("enabled", "Y" if account.enabled else "N")
        t.add_row("status", account.status)
        t.add_row("ws.status", ws_row.status if ws_row else "offline")
        t.add_row("ws.last_heartbeat_at", format_local(ws_row.last_heartbeat_at) or "-")
        t.add_row("ws.last_error", (ws_row.last_error or "-") if ws_row else "-")
        t.add_row("recent_msgs", str(len(recent)))
        for m in recent[:5]:
            ts = to_local(m.received_at).strftime("%H:%M:%S") if m.received_at else "-"
            t.add_row(f"  [{ts}] {m.direction}", (m.content or "")[:60])
        return t

    async def _run() -> None:
        with Live(await _snapshot(), refresh_per_second=1.0 / refresh, console=console) as live:
            try:
                while True:
                    await asyncio.sleep(refresh)
                    live.update(await _snapshot())
            except KeyboardInterrupt:
                pass

    asyncio.run(_run())
