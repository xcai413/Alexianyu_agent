"""`xianyu-agent message ...` subcommands."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.chat import ChatAssistService
from xianyu_agent.application.message import SendAttemptStatus, SendMessageService
from xianyu_agent.domain.message import messages as domain_messages
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.infrastructure.message import AuditLogMessageAttemptRecorder, DomainMessageStore
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.protocol.ws.message_adapter import WsClientMessageProtocol
from xianyu_agent.protocol.ws.message_read import MessageReadError
from xianyu_agent.runtime.account_lock import AccountConnectionAlreadyRunningError
from xianyu_agent.services.account_worker import AccountWorker
from xianyu_agent.utils.time_utils import format_local, to_local

app = typer.Typer(help="查询消息历史。")
quick_reply_app = typer.Typer(help="快捷回复:人工客服复用的话术(不参与自动回复)。")
note_app = typer.Typer(help="买家备注:只给运营看,不改变对买家说的话。")
filter_app = typer.Typer(help="消息过滤规则:按账号抑制自动回复或通知。")
app.add_typer(quick_reply_app, name="quick-reply")
app.add_typer(note_app, name="note")
app.add_typer(filter_app, name="filter")
console = Console()
SEND_CONNECT_TIMEOUT_S = 15.0
SEND_CONNECT_POLL_S = 0.05


class _SendWorkerUnavailable(RuntimeError):
    """The one-shot worker reached a definite pre-send terminal condition."""


@filter_app.command("add")
def add_message_filter(
    account_id: str = typer.Option(..., "--account", "-a"),
    keyword: str = typer.Option(..., "--keyword", "-k"),
    filter_type: str = typer.Option("skip_reply", "--type", help="skip_reply 或 skip_notify。"),
) -> None:
    """新增账号级消息过滤规则。"""

    async def _run() -> None:
        try:
            row = await ChatAssistService().create_message_filter(
                account_id=account_id, keyword=keyword, filter_type=filter_type
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 过滤规则 #{row.id} 已保存。")

    asyncio.run(_run())


@filter_app.command("list")
def list_message_filters(
    account_id: str = typer.Option("", "--account", "-a"),
    filter_type: str = typer.Option("", "--type"),
) -> None:
    """列出消息过滤规则。"""

    async def _run() -> None:
        try:
            rows = await ChatAssistService().list_message_filters(
                account_id=account_id or None, filter_type=filter_type or None
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        table = Table(title=f"消息过滤规则 ({len(rows)})")
        for column in ("id", "账号", "关键词", "类型", "启用"):
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(str(row.id), row.account_id, row.keyword, row.filter_type, "Y" if row.enabled else "N")
        console.print(table)

    asyncio.run(_run())


@filter_app.command("toggle")
def toggle_message_filter(filter_id: int = typer.Argument(...)) -> None:
    """切换一条消息过滤规则。"""

    async def _run() -> None:
        rows = await ChatAssistService().list_message_filters()
        row = next((item for item in rows if item.id == filter_id), None)
        if row is None or not await ChatAssistService().set_message_filter_enabled(filter_id, not row.enabled):
            console.print(f"[red]过滤规则 #{filter_id} 不存在[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 过滤规则 #{filter_id} 已{'启用' if not row.enabled else '停用'}。")

    asyncio.run(_run())


@filter_app.command("delete")
def delete_message_filter(filter_id: int = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """删除一条消息过滤规则。"""
    if not yes:
        console.print("[red]删除过滤规则不可恢复,请加 --yes。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        if not await ChatAssistService().delete_message_filter(filter_id):
            console.print(f"[red]过滤规则 #{filter_id} 不存在[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 过滤规则 #{filter_id} 已删除。")

    asyncio.run(_run())


@filter_app.command("batch-add")
def batch_add_message_filters(
    account_id: str = typer.Option(..., "--account", "-a"),
    keywords: list[str] = typer.Option(..., "--keyword", "-k"),  # noqa: B008
    filter_type: str = typer.Option("skip_reply", "--type"),
) -> None:
    """批量新增消息过滤规则,重复项会跳过。"""

    async def _run() -> None:
        try:
            created, skipped = await ChatAssistService().batch_create_message_filters(
                account_id=account_id, keywords=keywords, filter_type=filter_type
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 已创建 {created} 条,跳过 {skipped} 条。")

    asyncio.run(_run())


@filter_app.command("batch-delete")
def batch_delete_message_filters(
    filter_ids: list[int] = typer.Argument(...),  # noqa: B008
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """批量删除消息过滤规则。"""
    if not yes:
        console.print("[red]批量删除不可恢复,请加 --yes。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        deleted = await ChatAssistService().batch_delete_message_filters(filter_ids)
        console.print(f"[green]OK[/green] 已删除 {deleted} 条过滤规则。")

    asyncio.run(_run())


@quick_reply_app.command("add")
def add_quick_reply(
    title: str = typer.Option(..., "--title", "-t"),
    content: str = typer.Option(..., "--content", "-c", help="话术正文。"),
    account_id: str = typer.Option("", "--account", "-a", help="留空表示全局模板。"),
    sort_order: int = typer.Option(0, "--sort", min=0),
    category: str = typer.Option("默认", "--category"),
) -> None:
    """新增一条快捷回复(全局或账号专属)。"""

    async def _run() -> None:
        try:
            row = await ChatAssistService().add_quick_reply(
                title=title,
                content=content,
                account_id=account_id or None,
                sort_order=sort_order,
                category=category,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 快捷回复 #{row.id} {row.title} 已保存。")

    asyncio.run(_run())


@quick_reply_app.command("list")
def list_quick_replies(
    account_id: str = typer.Option("", "--account", "-a", help="含该账号专属模板与全局模板。"),
    include_disabled: bool = typer.Option(False, "--include-disabled", help="包含已停用短语。"),
) -> None:
    """列出快捷回复。"""

    async def _run() -> None:
        try:
            rows = await ChatAssistService().list_quick_replies(
                account_id=account_id or None, include_disabled=include_disabled
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if not rows:
            console.print("[dim]暂无快捷回复。[/dim]")
            return
        table = Table(title=f"快捷回复 ({len(rows)})")
        for column in ("id", "范围", "分类", "标题", "内容", "排序", "启用", "使用"):
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(
                str(row.id),
                row.account_id or "全局",
                row.category,
                row.title,
                row.content,
                str(row.sort_order),
                "Y" if row.enabled else "N",
                str(row.use_count),
            )
        console.print(table)

    asyncio.run(_run())


@quick_reply_app.command("update")
def update_quick_reply(  # noqa: PLR0917
    reply_id: int = typer.Argument(...),
    title: str | None = typer.Option(None, "--title", "-t"),
    content: str | None = typer.Option(None, "--content", "-c"),
    category: str | None = typer.Option(None, "--category"),
    sort_order: int | None = typer.Option(None, "--sort", min=0),
    enabled: bool | None = typer.Option(None, "--enabled/--disabled"),
) -> None:
    """更新快捷回复/短语。"""
    if all(value is None for value in (title, content, category, sort_order, enabled)):
        console.print("[red]至少提供一个要更新的字段[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            row = await ChatAssistService().update_quick_reply(
                reply_id, title=title, content=content, category=category,
                sort_order=sort_order, enabled=enabled
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if row is None:
            console.print(f"[red]快捷回复 #{reply_id} 不存在[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 快捷回复 #{reply_id} 已更新。")

    asyncio.run(_run())


@quick_reply_app.command("use")
def use_quick_reply(reply_id: int = typer.Argument(...)) -> None:
    """记录人工使用一次快捷短语(不发送消息)。"""

    async def _run() -> None:
        if not await ChatAssistService().use_quick_reply(reply_id):
            console.print(f"[red]快捷回复 #{reply_id} 不存在[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 快捷回复 #{reply_id} 使用次数已记录。")

    asyncio.run(_run())


@quick_reply_app.command("delete")
def delete_quick_reply(
    reply_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除一条快捷回复。"""
    if not yes:
        console.print("[red]删除快捷回复不可恢复,请加 --yes。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        removed = await ChatAssistService().delete_quick_reply(reply_id)
        if not removed:
            console.print(f"[red]快捷回复 #{reply_id} 不存在[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 快捷回复 #{reply_id} 已删除。")

    asyncio.run(_run())


@note_app.command("set")
def set_buyer_note(
    account_id: str = typer.Option(..., "--account", "-a"),
    buyer_id: str = typer.Option(..., "--buyer-id"),
    note: str = typer.Option(..., "--note", "-n", help="备注内容。"),
) -> None:
    """写入或更新买家备注(同账号同买家只有一条)。"""

    async def _run() -> None:
        try:
            row = await ChatAssistService().save_buyer_note(
                account_id=account_id, buyer_id=buyer_id, note=note
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 买家 {row.buyer_id} 的备注已保存。")

    asyncio.run(_run())


@note_app.command("show")
def show_buyer_note(
    account_id: str = typer.Option(..., "--account", "-a"),
    buyer_id: str = typer.Option(..., "--buyer-id"),
) -> None:
    """查看一个买家的备注。"""

    async def _run() -> None:
        try:
            row = await ChatAssistService().get_buyer_note(
                account_id=account_id, buyer_id=buyer_id
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if row is None:
            console.print("[dim]该买家没有备注。[/dim]")
            return
        console.print(f"[cyan]{row.buyer_id}[/cyan] {row.note}")
        console.print(f"[dim]更新于 {format_local(row.updated_at) or '-'}[/dim]")

    asyncio.run(_run())


@note_app.command("delete")
def delete_buyer_note(
    account_id: str = typer.Option(..., "--account", "-a"),
    buyer_id: str = typer.Option(..., "--buyer-id"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除买家备注。"""
    if not yes:
        console.print("[red]删除备注不可恢复,请加 --yes。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        removed = await ChatAssistService().delete_buyer_note(
            account_id=account_id, buyer_id=buyer_id
        )
        if not removed:
            console.print("[red]该买家没有备注[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 买家 {buyer_id} 的备注已删除。")

    asyncio.run(_run())


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
        for title in ("id", "时间", "方向", "chat_id", "sender", "type", "内容", "图片"):
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
                (row.image_url or "-")[:60],
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


def _require_conversation(row: object | None, *, account_id: str, chat_id: str) -> None:
    if row is not None:
        return
    console.print(f"[red]未找到账号 {account_id} 的会话 {chat_id}。[/red]")
    raise typer.Exit(code=1)


@app.command("takeover")
def takeover_conversation(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
) -> None:
    """转人工接管一个会话,停止该会话自动回复。"""

    async def _run() -> None:
        row = await domain_messages.set_takeover_state(
            account_id=account_id, chat_id=chat_id, state="human"
        )
        _require_conversation(row, account_id=account_id, chat_id=chat_id)
        console.print(f"[green]OK[/green] 会话 {chat_id} 已转人工接管。")

    asyncio.run(_run())


@app.command("release")
def release_conversation(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
) -> None:
    """释放人工接管,恢复该会话的机器人所有权。"""

    async def _run() -> None:
        row = await domain_messages.set_takeover_state(
            account_id=account_id, chat_id=chat_id, state="bot"
        )
        _require_conversation(row, account_id=account_id, chat_id=chat_id)
        console.print(f"[green]OK[/green] 会话 {chat_id} 已恢复机器人接管。")

    asyncio.run(_run())


@app.command("pause-reply")
def pause_reply(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
) -> None:
    """暂停一个会话的自动回复。"""

    async def _run() -> None:
        row = await domain_messages.set_bot_state(
            account_id=account_id, chat_id=chat_id, state="paused"
        )
        _require_conversation(row, account_id=account_id, chat_id=chat_id)
        console.print(f"[green]OK[/green] 会话 {chat_id} 的自动回复已暂停。")

    asyncio.run(_run())


@app.command("resume-reply")
def resume_reply(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
) -> None:
    """恢复一个会话的自动回复。"""

    async def _run() -> None:
        row = await domain_messages.set_bot_state(
            account_id=account_id, chat_id=chat_id, state="enabled"
        )
        _require_conversation(row, account_id=account_id, chat_id=chat_id)
        console.print(f"[green]OK[/green] 会话 {chat_id} 的自动回复已恢复。")

    asyncio.run(_run())


@app.command("mark-read")
def mark_read(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
) -> None:
    """把会话的本地未读计数清零(纯本地记账,不调用平台)。

    平台上"已读"是另一件事:需要显式 `message report-read --yes` 上报。两者分开是为了避免
    本地假装平台状态已经改变。
    """

    async def _run() -> None:
        previous = await domain_messages.mark_conversation_read(
            account_id=account_id, chat_id=chat_id
        )
        if previous is None:
            console.print(f"[red]账号 {account_id} 下没有会话 {chat_id}[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[green]OK[/green] 会话 {chat_id} 本地未读已清零(原为 {previous} 条)。"
        )
        console.print("[dim]平台侧未读标记需要 `message report-read --yes` 才会变化。[/dim]")

    asyncio.run(_run())


@app.command("report-read")
def report_read(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认向平台上报已读(真实平台动作)。"),
) -> None:
    """向平台上报该会话的入站消息已读,成功后清零本地未读计数(未真实校准)。

    帧形状来自参考项目 Helper 的 `/r/MessageStatus/read`;它不向买家发送任何内容。
    """
    if not yes:
        console.print("[red]这会真实改变平台未读状态,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        rows = await domain_messages.list_unread_messages(
            account_id=account_id, chat_id=chat_id
        )
        if not rows:
            console.print("[yellow]该会话没有带平台消息 ID 的入站消息,无需上报。[/yellow]")
            return
        # 只上报一部分却清零本地计数会造成"本地说已读、平台还有未读"的假象,所以先判断截断。
        more_than_reported = await domain_messages.has_more_inbound_messages_with_ids(
            account_id=account_id, chat_id=chat_id
        )
        worker = AccountWorker(account_id)
        try:
            startup = worker.start()
        except AccountConnectionAlreadyRunningError:
            console.print(
                "[red]该账号已有常驻连接:请由 daemon 上报,CLI 一次性上报不可用。[/red]"
            )
            raise typer.Exit(code=1) from None
        try:
            if startup is not None:
                try:
                    await startup
                except Exception as exc:
                    console.print(
                        f"[red]上报前失败:[/red] Worker 启动失败 ({type(exc).__name__})"
                    )
                    raise typer.Exit(code=1) from exc
            try:
                await _wait_for_protocol_ready(worker)
            except (_SendWorkerUnavailable, TimeoutError) as exc:
                console.print(f"[red]上报前失败:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            try:
                await WsClientMessageProtocol(worker._client).report_read(
                    chat_id=chat_id,
                    message_ids=[str(row.message_id) for row in rows],
                )
            except MessageReadError as exc:
                console.print(f"[red]已读上报失败:[/red] {exc}")
                raise typer.Exit(code=1) from exc
        finally:
            await worker.stop()
        await domain_messages.mark_conversation_read(account_id=account_id, chat_id=chat_id)
        console.print(
            f"[green]OK[/green] 已上报 {len(rows)} 条消息已读,本地未读计数已清零。"
        )
        if more_than_reported:
            console.print(
                "[yellow]注意[/yellow] 该会话还有更多历史入站消息未上报;"
                "本次仍清零了本地计数,如需完整上报请重复执行本命令。"
            )

    asyncio.run(_run())


@app.command("send-image")
def send_image(  # noqa: PLR0917 - Typer 命令参数就是公开配置面
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
    image_url: str = typer.Option(..., "--image-url", help="https 公网图片地址。"),
    receiver_id: str = typer.Option("", "--receiver-id", "-r", help="通常可留空,由会话镜像解析。"),
    width: int = typer.Option(800, "--width", min=1, max=10000),
    height: int = typer.Option(600, "--height", min=1, max=10000),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认向买家真实发送图片。"),
) -> None:
    """向一个会话发送图片消息(真实外发,未真实校准)。

    图片帧与文本共用一个信封,字段形状来自参考项目 Helper 的 SendImage;真实发送前请先按
    `docs/待真实验证清单.md` F9 做单会话校准。
    """
    if not yes:
        console.print("[red]这会向买家真实发送图片,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        store = DomainMessageStore()
        receiver = receiver_id.strip() or await store.resolve_receiver(
            account_id=account_id, chat_id=chat_id
        )
        if receiver is None:
            console.print("[red]发送失败:账号未连接或当前会话无法解析接收方。[/red]")
            raise typer.Exit(code=1)
        worker = AccountWorker(account_id)
        try:
            startup = worker.start()
        except AccountConnectionAlreadyRunningError:
            console.print(
                "[red]该账号已有常驻 WebSocket 连接:请由 daemon 执行发送,CLI 一次性发送不可用。[/red]"
            )
            raise typer.Exit(code=1) from None
        try:
            if startup is not None:
                try:
                    await startup
                except Exception as exc:
                    console.print(
                        f"[red]发送前失败,消息未写出:[/red] Worker 启动失败 ({type(exc).__name__})"
                    )
                    raise typer.Exit(code=1) from exc
            try:
                await _wait_for_protocol_ready(worker)
            except _SendWorkerUnavailable as exc:
                console.print(f"[red]发送前失败,消息未写出:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            except TimeoutError as exc:
                console.print("[red]发送前失败,可安全重试:[/red] 等待 WebSocket 协议会话就绪超时。")
                raise typer.Exit(code=1) from exc
            service = SendMessageService(
                WsClientMessageProtocol(worker._client),
                AuditLogMessageAttemptRecorder(),
                store,
            )
            result = await service.send_image(
                account_id=account_id,
                chat_id=chat_id,
                receiver_id=receiver,
                image_url=image_url,
                width=width,
                height=height,
            )
        finally:
            await worker.stop()

        if result.status is SendAttemptStatus.SUCCESS:
            console.print(f"[green]OK[/green] 已向 chat={chat_id} 发送图片 {image_url}")
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


@app.command("send")
def send_message(
    account_id: str = typer.Option(..., "--account", "-a"),
    chat_id: str = typer.Option(..., "--chat-id", "-c"),
    text: str = typer.Option(..., "--text", "-t"),
    receiver_id: str = typer.Option("", "--receiver-id", "-r"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认向买家真实发送这条消息。"),
) -> None:
    """一次性向一个会话发送文本(真实外发,买家会收到)。

    这是人工/校准用的单次入口,不是自动回复:自动化走 daemon 里的回复引擎,受
    `XIANYU_AUTOMATION_MODE` 与接管状态约束。这里要求 `--yes` 逐次确认,并且需要该账号没有
    常驻 Worker(同一账号不能同时开两个 WebSocket 会话);发送事实写审计,失败按
    「确定未发出 / 明确拒绝 / 结果不明」分类,结果不明时拒绝直接重试。
    """
    if not yes:
        console.print("[red]这会向买家真实发送消息,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)

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
        try:
            startup = worker.start()
        except AccountConnectionAlreadyRunningError as exc:
            console.print(
                "[red]发送失败:该账号已有常驻 WebSocket 连接。[/red] "
                "当前 `message send` 为一次性发送命令,不能跨进程复用 daemon Worker;"
                f"请先执行 `xianyu-agent pool stop --account {account_id}` 后重试。"
            )
            raise typer.Exit(code=2) from exc

        try:
            if startup is not None:
                try:
                    await startup
                except Exception as exc:
                    console.print(
                        f"[red]发送前失败,消息未写出:[/red] Worker 启动失败 ({type(exc).__name__})"
                    )
                    raise typer.Exit(code=1) from exc
            try:
                await _wait_for_protocol_ready(worker)
            except _SendWorkerUnavailable as exc:
                console.print(f"[red]发送前失败,消息未写出:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            except TimeoutError as exc:
                console.print("[red]发送前失败,可安全重试:[/red] 等待 WebSocket 协议会话就绪超时。")
                raise typer.Exit(code=1) from exc

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


async def _wait_for_protocol_ready(
    worker: AccountWorker,
    *,
    timeout_s: float = SEND_CONNECT_TIMEOUT_S,
) -> None:
    """Wait for a business-ready WS session or a definite pre-send terminal state."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_s)
    while True:
        lifecycle_error = worker.lifecycle_error
        if lifecycle_error is not None:
            raise _SendWorkerUnavailable(
                "Worker 生命周期失败 "
                f"({type(lifecycle_error).__name__});请先检查账号认证状态与运行日志后再发送。"
            )
        if worker.state is WorkerState.NEEDS_VALIDATION:
            raise _SendWorkerUnavailable(
                "账号需要人工验证;请先完成验证并确认认证状态有效后再发送。"
            )
        if worker.state is WorkerState.ERROR:
            raise _SendWorkerUnavailable(
                "账号 Worker 已进入 ERROR;请先检查账号认证状态与运行日志后再发送。"
            )
        if worker._client.state is ConnectionState.CONNECTED:
            return
        if loop.time() >= deadline:
            raise TimeoutError("WebSocket protocol session readiness timed out")
        await asyncio.sleep(SEND_CONNECT_POLL_S)


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
