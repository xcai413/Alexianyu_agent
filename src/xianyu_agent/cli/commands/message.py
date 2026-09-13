"""`xianyu-agent message ...` subcommands."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.message import SendAttemptStatus
from xianyu_agent.domain.message import messages as domain_messages
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
    receiver_id: str = typer.Option(..., "--receiver-id", "-r", help="买家闲鱼用户 ID。"),
    text: str = typer.Option(..., "--text", "-t", help="消息内容。"),
) -> None:
    """通过统一 SendMessageService 手动发送一条文字消息。"""

    async def _run() -> None:
        worker = AccountWorker(account_id)
        startup = worker.start()
        try:
            if startup is not None:
                await startup
            result = await worker.send_message(
                chat_id=chat_id,
                receiver_id=receiver_id,
                text=text,
            )
        finally:
            await worker.stop()

        if result.status is SendAttemptStatus.SUCCESS:
            await domain_messages.record_outbound(
                MessageSent(
                    event_id=result.client_message_id or "manual",
                    account_id=account_id,
                    received_at=datetime.now(UTC),
                    chat_id=chat_id,
                    receiver_id=receiver_id,
                    content_type=MessageContentType.TEXT,
                    content=text,
                )
            )
            console.print(f"[green]OK[/green] 已发送到 chat={chat_id}: {text[:40]}")
            return

        if result.status is SendAttemptStatus.UNCERTAIN:
            console.print(
                "[yellow]发送结果不确定。[/yellow] 请求可能已到达闲鱼，禁止直接重试；"
                "请先通过消息历史做 reconciliation。"
            )
        elif result.status is SendAttemptStatus.RECONCILIATION_REQUIRED:
            console.print(
                "[yellow]平台发送已成功，但本地结果审计未完整落盘。[/yellow] "
                "禁止重复发送，请执行 reconciliation。"
            )
        elif result.status is SendAttemptStatus.FAILED_RETRYABLE:
            console.print(f"[red]发送前失败，可在恢复连接后重试：[/red] {result.detail or '-'}")
        else:
            console.print(f"[red]发送被拒绝/最终失败：[/red] {result.detail or '-'}")
        raise typer.Exit(code=1)

    asyncio.run(_run())
