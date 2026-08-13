"""`xianyu-agent message ...` subcommands."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import messages as domain_messages
from xianyu_agent.protocol.events import MessageContentType, MessageSent
from xianyu_agent.services.account_worker import AccountWorker
from xianyu_agent.utils.time_utils import format_local, to_local

app = typer.Typer(help="查询消息历史。")
console = Console()


@app.command("list")
def list_messages(
    account_id: str = typer.Option(..., "--account", "-a"),
    since: str = typer.Option("1h", "--since", help="时间窗,例如 10m / 2h / 1d。"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
    direction: str = typer.Option("all", "--direction", help="all / inbound / outbound。"),
    chat_id: str = typer.Option("", "--chat-id", help="过滤特定会话。"),
) -> None:
    """列出某账号近期消息。"""
    since_dt = _parse_since(since)
    if since_dt is None:
        console.print(f"[red]无法解析 --since={since}[/red],应为 10m / 2h / 1d 形式。")
        raise typer.Exit(code=1)
    direction_arg = None if direction == "all" else direction
    chat_arg = chat_id or None

    async def _run() -> None:
        rows = await domain_messages.list_recent(
            account_id=account_id,
            since=since_dt,
            limit=limit,
            direction=direction_arg,
            chat_id=chat_arg,
        )
        if not rows:
            console.print(f"[dim]账号 {account_id} 在 {since} 内无消息。[/dim]")
            return
        table = Table(title=f"{account_id} (since {since})")
        table.add_column("id", style="dim")
        table.add_column("时间", style="cyan")
        table.add_column("方向")
        table.add_column("chat_id")
        table.add_column("sender")
        table.add_column("type")
        table.add_column("内容", overflow="fold")
        for r in rows:
            ts = to_local(r.received_at)
            table.add_row(
                str(r.id),
                ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-",
                r.direction,
                r.chat_id[:18] if r.chat_id else "-",
                (r.sender_name or r.sender_id or "-")[:18],
                r.content_type,
                (r.content or "")[:80],
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_message(
    message_id: int = typer.Argument(..., help="消息数据库 ID。"),
) -> None:
    """显示单条消息的全文与原始 payload。"""

    async def _run() -> None:
        m = await domain_messages.get_by_id(message_id)
        if m is None:
            console.print(f"[red]未找到 message_id={message_id}[/red]")
            raise typer.Exit(code=1)
        console.rule(f"消息 #{m.id}")
        console.print(f"账号 ID:    {m.account_id}")
        console.print(f"chat_id:    {m.chat_id}")
        console.print(f"sender:     {m.sender_name or '-'}  ({m.sender_id or '-'})")
        console.print(f"方向:       {m.direction}")
        console.print(f"类型:       {m.content_type}")
        console.print(f"received:   {format_local(m.received_at) or '-'}")
        console.print(f"\n[bold]内容:[/bold]\n{m.content or '(空)'}")
        if m.image_url:
            console.print(f"\n图片: {m.image_url}")
        if m.raw_payload:
            console.print("\n[dim]原始 payload:[/dim]")
            console.print_json(json.dumps(m.raw_payload, ensure_ascii=False, indent=2))

    asyncio.run(_run())


def _parse_since(s: str) -> datetime | None:
    """Parse strings like 30s / 10m / 2h / 1d."""
    if not s:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        n = int(s[:-1])
        unit = s[-1].lower()
    except (ValueError, IndexError):
        return None
    if unit not in units:
        return None
    return datetime.now(UTC) - timedelta(seconds=n * units[unit])


@app.command("send")
def send_message(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c", help="会话 ID。"),
    text: str = typer.Option(..., "--text", "-t", help="消息内容。"),
) -> None:
    """手动发送一条消息(需要 Worker 在线;离线会如实失败)。"""

    async def _run() -> None:
        worker = AccountWorker(account_id)
        worker.start()
        try:
            ok = await worker.send_text(text)
        finally:
            await worker.stop()
        if not ok:
            console.print(
                "[red]发送失败:账号未连接(离线)。[/red] 先确认 XIANYU_WS_URL 已配置且 "
            "daemon run 和对应账号的 pool start 已执行。"
            )
            raise typer.Exit(code=1)
        await domain_messages.record_outbound(
            MessageSent(
                event_id="manual",
                account_id=account_id,
                received_at=datetime.now(UTC),
                chat_id=chat_id,
                receiver_id="",
                content_type=MessageContentType.TEXT,
                content=text,
            )
        )
        console.print(f"[green]OK[/green] 已发送到 chat={chat_id}: {text[:40]}")

    asyncio.run(_run())
