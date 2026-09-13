"""`xianyu-agent message ...` subcommands."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.message import SendAttemptStatus, SendMessageService
from xianyu_agent.domain.message import messages as domain_messages
from xianyu_agent.infrastructure.message import AuditLogMessageAttemptRecorder, DomainMessageStore
from xianyu_agent.protocol.ws.message_adapter import WsClientMessageProtocol
from xianyu_agent.services.account_worker import AccountWorker
from xianyu_agent.utils.time_utils import format_local, to_local

app = typer.Typer(help="查询消息历史。")
console = Console()


@app.command("list")
def list_messages(
    account_id: str = typer.Option(..., "--account", "-a"),
    since: str = typer.Option("1h", "--since"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
    direction: str = typer.Option("all", "--direction"),
    chat_id: str = typer.Option("", "--chat-id"),
) -> None:
    since_dt = _parse_since(since)
    if since_dt is None:
        console.print(f"[red]无法解析 --since={since}[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        rows = await domain_messages.list_recent(
            account_id=account_id,
            since=since_dt,
            limit=limit,
            direction=None if direction == "all" else direction,
            chat_id=chat_id or None,
        )
        if not rows:
            console.print(f"[dim]账号 {account_id} 在 {since} 内无消息。[/dim]")
            return
        table = Table(title=f"{account_id} (since {since})")
        for title in ("id", "时间", "方向", "chat_id", "sender", "type", "内容"):
            table.add_column(title)
        for row in rows:
            ts = to_local(row.received_at)
            table.add_row(
                str(row.id),
                ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "-",
                row.direction,
                row.chat_id[:18] if row.chat_id else "-",
                (row.sender_name or row.sender_id or "-")[:18],
                row.content_type,
                (row.content or "")[:80],
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_message(message_id: int = typer.Argument(...)) -> None:
    async def _run() -> None:
        row = await domain_messages.get_by_id(message_id)
        if row is None:
            console.print(f"[red]未找到 message_id={message_id}[/red]")
            raise typer.Exit(code=1)
        console.rule(f"消息 #{row.id}")
        console.print(f"账号 ID:    {row.account_id}")
        console.print(f"chat_id:    {row.chat_id}")
        console.print(f"sender:     {row.sender_name or '-'}  ({row.sender_id or '-'})")
        console.print(f"方向:       {row.direction}")
        console.print(f"类型:       {row.content_type}")
        console.print(f"received:   {format_local(row.received_at) or '-'}")
        console.print(f"\n[bold]内容:[/bold]\n{row.content or '(空)'}")
        if row.image_url:
            console.print(f"\n图片: {row.image_url}")
        if row.raw_payload:
            console.print_json(json.dumps(row.raw_payload, ensure_ascii=False, indent=2))

    asyncio.run(_run())


@app.command("send")
def send_message(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
    text: str = typer.Option(..., "--text", "-t"),
    receiver_id: str = typer.Option("", "--receiver-id", "-r"),
) -> None:
    async def _run() -> None:
        store = DomainMessageStore()
        receiver = receiver_id.strip() or await store.resolve_receiver(
            account_id=account_id,
            chat_id=chat_id,
        )
        if receiver is None:
            console.print("[red]发送失败:账号未连接或当前会话无法解析接收方。[/red]")
            raise typer.Exit(code=1)

        worker = AccountWorker(account_id)
        startup = worker.start()
        try:
            if startup is not None:
                await startup
            service = SendMessageService(
                WsClientMessageProtocol(worker._client),
                AuditLogMessageAttemptRecorder(),
                store,
            )
            result = await service.send_text(
                account_id=account_id,
                chat_id=chat_id,
                receiver_id=receiver,
                text=text,
            )
        finally:
            await worker.stop()

        if result.status is SendAttemptStatus.SUCCESS:
            console.print(f"[green]OK[/green] 已发送到 chat={chat_id}: {text[:40]}")
            return
        if result.status is SendAttemptStatus.UNCERTAIN:
            console.print("[yellow]发送结果不确定;禁止直接重试,请先核对消息历史。[/yellow]")
        elif result.status is SendAttemptStatus.RECONCILIATION_REQUIRED:
            console.print("[yellow]外部已成功但本地需要 reconciliation;禁止重复发送。[/yellow]")
        elif result.status is SendAttemptStatus.FAILED_RETRYABLE:
            console.print(f"[red]发送前失败,可重试:[/red] {result.detail or '-'}")
        else:
            console.print(f"[red]发送失败:[/red] {result.detail or '-'}")
        raise typer.Exit(code=1)

    asyncio.run(_run())


def _parse_since(value: str) -> datetime | None:
    if not value:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        amount = int(value[:-1])
        factor = units[value[-1].lower()]
    except (ValueError, IndexError, KeyError):
        return None
    return datetime.now(UTC) - timedelta(seconds=amount * factor)
