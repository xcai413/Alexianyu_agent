"""`xianyu-agent protocol ...` subcommands (Phase 1)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
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
    accounts as domain_accounts,
    messages as domain_messages,
    orders as domain_orders,
)
from xianyu_agent.protocol.capture import CalibrationRecorder, verify_capture
from xianyu_agent.protocol.client import WsClient
from xianyu_agent.protocol.events import (
    MessageReceived,
    OrderCreated,
    OrderDelivered,
    OrderPaid,
    SystemNotice,
    WsFrame,
)
from xianyu_agent.services.account_lock import AccountConnectionAlreadyRunningError
from xianyu_agent.services.account_worker import AccountWorker
from xianyu_agent.utils.time_utils import format_local, to_local

app = typer.Typer(help="协议层命令:连接 / 测试 / 录制回放。")
console = Console()
logger = logging.getLogger(__name__)


@app.command("capture-verify")
def capture_verify(
    fixture: str = typer.Option(..., "--fixture", "-f", help="capture 生成的 JSONL。"),
) -> None:
    """离线验证脱敏 capture 是否达到 P1 消息校准门槛。"""
    result = verify_capture(Path(fixture))
    table = Table(title="P1 捕获验收")
    table.add_column("字段", style="cyan")
    table.add_column("值")
    table.add_row("result", "PASS" if result.ok else "FAIL")
    table.add_row("records", str(result.records))
    table.add_row("frames/events/messages", f"{result.frames}/{result.events}/{result.messages}")
    table.add_row("target_reached", "Y" if result.target_reached else "N")
    table.add_row("missing_fields", ",".join(result.missing_message_fields) or "-")
    table.add_row("issues", "; ".join(result.issues) or "-")
    console.print(table)
    if not result.ok:
        raise typer.Exit(code=1)


@app.command("capture")
def capture(  # noqa: PLR0915
    account_id: str = typer.Option(..., "--account", "-a"),
    seconds: float = typer.Option(300.0, "--seconds", "-s", min=1.0),
    output: str = typer.Option("", "--output", "-o", help="脱敏 JSONL 路径。"),
    target_messages: int = typer.Option(
        1, "--target-messages", min=0, help="收到该数量买家消息后提前结束;0=只按时长。"
    ),
) -> None:
    """前台捕获真实推送并写入不可逆脱敏 fixture;不执行回复或发货。"""

    async def _run() -> None:  # noqa: PLR0915
        account = await domain_accounts.get_account(account_id)
        if account is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        if account.desired_state == "running":
            console.print(
                "[red]同账号 daemon Worker 仍期望 running。[/red] 请先执行 "
                f"[cyan]pool stop --account {account_id}[/cyan]。"
            )
            raise typer.Exit(code=2)
        path = (
            Path(output)
            if output
            else get_settings().data_dir
            / "captures"
            / f"{account_id}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.jsonl"
        )
        recorder = CalibrationRecorder(path, account_id=account_id)
        counts = {"message": 0, "order": 0, "system": 0, "error": 0}
        connected = asyncio.Event()
        finished = asyncio.Event()
        terminal_error = False
        seen_message_ids: set[str] = set()
        duplicate_message_events = 0
        missing_fields: set[str] = set()

        async def on_event(event) -> None:
            nonlocal duplicate_message_events
            await recorder.record_event(event)
            persisted = event.model_copy(update={"raw": None})
            if isinstance(event, MessageReceived):
                counts["message"] += 1
                required = {
                    "message_id": event.message_id,
                    "chat_id": event.chat_id,
                    "buyer_id": event.sender_id,
                    "item_id": event.item_id,
                    "sent_at": event.sent_at,
                    "content": event.content,
                }
                missing_fields.update(name for name, value in required.items() if not value)
                if event.message_id:
                    if event.message_id in seen_message_ids:
                        duplicate_message_events += 1
                    seen_message_ids.add(event.message_id)
                await domain_messages.upsert_inbound(persisted)
                console.print(
                    "[cyan]MSG[/cyan] 已收到并落库:"
                    f"chat={_masked(event.chat_id)} message={_masked(event.message_id)} "
                    f"item={_masked(event.item_id)}"
                )
                if target_messages > 0 and counts["message"] >= target_messages:
                    finished.set()
            elif isinstance(event, (OrderCreated, OrderPaid, OrderDelivered)):
                counts["order"] += 1
                await domain_orders.upsert_from_event(persisted)
                console.print(f"[magenta]ORDER[/magenta] {type(event).__name__} 已落库")
            elif isinstance(event, SystemNotice):
                counts["system"] += 1
                console.print(f"[yellow]SYSTEM[/yellow] {event.notice_type}")

        async def on_state(state) -> None:
            await recorder.record_state(state)
            console.print(f"[dim]STATE {state.state.value} {state.detail or ''}[/dim]")
            if state.state.value == "connected":
                connected.set()

        async def on_error(error) -> None:
            nonlocal terminal_error
            await recorder.record_error(error)
            counts["error"] += 1
            if error.code in {"duplicate_connection", "ws_auth"}:
                terminal_error = True
                finished.set()
            console.print(f"[red]ERROR {error.code}:{error.message}[/red]")

        client = WsClient(
            account_id,
            on_event=on_event,
            on_frame=recorder.record_frame,
            on_state=on_state,
            on_error=on_error,
        )
        console.print(
            f"[bold]观察模式[/bold]:{seconds:.0f} 秒;不自动回复/发货;脱敏证据:{path}"
        )
        try:
            client.start()
        except AccountConnectionAlreadyRunningError as exc:
            console.print(f"[red]账号连接已被占用:{exc}[/red]")
            raise typer.Exit(code=2) from exc
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(finished.wait(), timeout=seconds)
        finally:
            await client.stop()
        summary = {
            "connected": connected.is_set(),
            "frames": recorder.counts.frames,
            "events": recorder.counts.events,
            "messages": counts["message"],
            "orders": counts["order"],
            "system_notices": counts["system"],
            "errors": counts["error"],
            "duplicate_message_events": duplicate_message_events,
            "missing_message_fields": sorted(missing_fields),
            "target_messages": target_messages,
            "target_reached": target_messages == 0 or counts["message"] >= target_messages,
        }
        recorder.record_summary(summary)
        console.print(
            "[bold]捕获汇总[/bold]:"
            f"connected={'Y' if connected.is_set() else 'N'} frames={recorder.counts.frames} "
            f"events={recorder.counts.events} messages={counts['message']} "
            f"orders={counts['order']} system={counts['system']} errors={counts['error']}"
        )
        if missing_fields:
            console.print(f"[yellow]字段缺失:{','.join(sorted(missing_fields))}[/yellow]")
        if terminal_error or not connected.is_set():
            raise typer.Exit(code=2)
        if target_messages > 0 and counts["message"] < target_messages:
            console.print(
                f"[red]未达到目标消息数:{counts['message']}/{target_messages}[/red]"
            )
            raise typer.Exit(code=2)
        if missing_fields:
            raise typer.Exit(code=3)

    asyncio.run(_run())


def _masked(value: str | None) -> str:
    if not value:
        return "-"
    return f"{value[:4]}...{uuid.uuid5(uuid.NAMESPACE_OID, value).hex[:6]}"


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
        try:
            client.start()
        except AccountConnectionAlreadyRunningError as exc:
            console.print(f"[red]账号连接已被占用:{exc}[/red]")
            raise typer.Exit(code=2) from exc
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
    """从 fixture 注入帧(用于纯离线测试 parser 与入库)。"""
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
        try:
            for _ in range(count):
                for f in raw_frames:
                    worker.inject_frame(WsFrame.model_validate(f))
            await worker.drain_injected_frames()
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
