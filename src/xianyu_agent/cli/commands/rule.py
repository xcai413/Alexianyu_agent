"""`xianyu-agent rule ...` subcommands: reply rule CRUD + dry-run test."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain.item import items as domain_items
from xianyu_agent.domain.message import rules as domain_rules
from xianyu_agent.domain.message.rules import DEFAULT_PRIORITY
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="回复规则(关键词 / 正则 / 默认)。")
console = Console()


@app.command("add")
def add_rule(  # noqa: PLR0917 (CLI option surface)
    name: str = typer.Option(..., "--name", "-n", help="规则名。"),
    type_: str = typer.Option("keyword", "--type", "-t", help="keyword | regex | default"),
    pattern: str = typer.Option("", "--pattern", "-p", help="匹配模式(默认规则可留空)。"),
    reply: str = typer.Option(..., "--reply", "-r", help="回复内容。"),
    account_id: str = typer.Option("", "--account", "-a", help="限定账号;留空=全局。"),
    item_pk: int | None = typer.Option(None, "--item", help="限定本地商品镜像 ID。"),
    priority: int = typer.Option(DEFAULT_PRIORITY, "--priority", help="越小越优先。"),
    reply_once: bool = typer.Option(False, "--reply-once", help="同一会话仅成功自动回复一次。"),
    reply_image: str = typer.Option(
        "", "--reply-image", help="随回复一起发送的 https 图片地址(可选)。"
    ),
) -> None:
    """新建回复规则。"""

    async def _run() -> None:
        try:
            row = await domain_rules.create_rule(
                name,
                type_,
                pattern,
                reply,
                account_id=account_id or None,
                item_pk=item_pk,
                priority=priority,
                reply_once=reply_once,
                reply_image_url=reply_image or None,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        scope = f"商品 #{item_pk}" if item_pk is not None else account_id or "全局"
        console.print(
            f"[green]OK[/green] 规则 #{row.id} [cyan]{row.name}[/cyan] "
            f"({row.type},{scope},priority={row.priority})"
        )

    asyncio.run(_run())


@app.command("list")
def list_rules(
    account_id: str = typer.Option("", "--account", "-a", help="只看某账号的规则。"),
    item_pk: int | None = typer.Option(None, "--item", help="只看某本地商品镜像的规则。"),
) -> None:
    """列出规则。"""

    async def _run() -> None:
        rows = await domain_rules.list_rules(account_id=account_id or None, item_pk=item_pk)
        if not rows:
            console.print("[dim]暂无规则,先 `rule add`。[/dim]")
            return
        table = Table(title=f"规则列表 ({len(rows)})")
        table.add_column("id")
        table.add_column("name", style="cyan")
        table.add_column("type")
        table.add_column("pattern", overflow="fold")
        table.add_column("reply", overflow="fold")
        table.add_column("scope")
        table.add_column("priority")
        table.add_column("enabled")
        table.add_column("once")
        table.add_column("图片")
        table.add_column("hits")
        for r in rows:
            scope = (
                f"商品 #{r.item_pk}"
                if r.item_pk is not None
                else "全局"
                if r.account_id is None
                else str(r.account_id)
            )
            table.add_row(
                str(r.id),
                r.name,
                r.type,
                r.pattern or "-",
                (r.reply_text or "")[:40],
                scope,
                str(r.priority),
                "Y" if r.enabled else "N",
                "Y" if r.reply_once else "N",
                "Y" if getattr(r, "reply_image_url", None) else "N",
                str(r.hit_count),
            )
        console.print(table)

    asyncio.run(_run())


@app.command("enable")
def enable_rule(rule_id: int = typer.Argument(...)) -> None:
    """启用规则。"""

    async def _run() -> None:
        ok = await domain_rules.set_rule_enabled(rule_id, True)
        if not ok:
            console.print(f"[red]规则 #{rule_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 规则 #{rule_id} 已启用。")

    asyncio.run(_run())


@app.command("disable")
def disable_rule(rule_id: int = typer.Argument(...)) -> None:
    """禁用规则。"""

    async def _run() -> None:
        ok = await domain_rules.set_rule_enabled(rule_id, False)
        if not ok:
            console.print(f"[red]规则 #{rule_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]OK[/yellow] 规则 #{rule_id} 已禁用。")

    asyncio.run(_run())


@app.command("delete")
def delete_rule(
    rule_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """删除规则。"""
    if not yes:
        confirm = typer.confirm(f"确认删除规则 #{rule_id}?")
        if not confirm:
            raise typer.Abort()

    async def _run() -> None:
        ok = await domain_rules.delete_rule(rule_id)
        if not ok:
            console.print(f"[red]规则 #{rule_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[red]OK[/red] 规则 #{rule_id} 已删除。")

    asyncio.run(_run())


@app.command("test")
def dry_run(
    content: str = typer.Option(..., "--content", "-c", help="要测试的消息内容。"),
    account_id: str = typer.Option("", "--account", "-a", help="按某账号范围匹配。"),
    item_pk: int | None = typer.Option(None, "--item", help="按本地商品镜像模拟匹配。"),
) -> None:
    """干跑:显示哪些规则会命中,不发消息。"""

    async def _run() -> None:
        target = account_id or "__global__"
        item_id = None
        if item_pk is not None:
            if not account_id:
                raise typer.BadParameter("指定 --item 时必须同时指定 --account")
            item = await domain_items.get_item(item_pk)
            if item is None or item.account_id is None:
                raise typer.BadParameter(f"商品镜像 #{item_pk} 不存在")
            item_id = item.item_id
        rows = await domain_rules.match_for_account(target, content, item_id=item_id)
        if not rows:
            console.print("[dim]没有规则命中。[/dim]")
            return
        table = Table(title=f"命中 ({len(rows)})")
        table.add_column("id")
        table.add_column("name", style="cyan")
        table.add_column("type")
        table.add_column("pattern")
        table.add_column("reply", overflow="fold")
        for r in rows:
            table.add_row(str(r.id), r.name, r.type, r.pattern or "-", r.reply_text)
        console.print(table)

    asyncio.run(_run())


@app.command("logs")
def list_reply_logs(
    account_id: str = typer.Option(..., "--account", "-a"),
    rule_id: int | None = typer.Option(None, "--rule-id", help="只看某条规则。"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """列出回复投递记录(脱敏预览);这也是 `--reply-once` 的判重依据。"""

    async def _run() -> None:
        try:
            rows = await domain_rules.list_reply_logs(
                account_id, rule_id=rule_id, limit=limit
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if not rows:
            console.print("[dim]还没有回复投递记录。[/dim]")
            return
        table = Table(title=f"回复投递记录 ({len(rows)})")
        for column in ("id", "规则", "会话", "结果", "来源", "正文长度", "正文预览", "图片", "时间"):
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(
                str(row.id),
                f"#{row.rule_id}({row.rule_name or '-'})" if row.rule_id else "-",
                str(row.conversation_id) if row.conversation_id is not None else "-",
                "OK" if row.success else f"FAIL:{row.error or '-'}",
                row.source,
                str(row.text_length),
                row.text_preview or "-",
                "Y" if row.has_image else "N",
                format_local(row.sent_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("reset-records")
def reset_reply_records(
    account_id: str = typer.Option(..., "--account", "-a"),
    rule_id: int | None = typer.Option(None, "--rule-id", help="只清某条规则的记录。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认清空。"),
) -> None:
    """清空回复投递记录(纯本地,不调用平台)。

    用途:`--reply-once` 规则靠这份记录判重,校准或测试用掉之后同一个会话不会再触发,重置一次就能
    重新验证。这里只删去重记录——规则、消息与审计都不动,清空动作本身会写一条审计。
    """
    if not yes:
        target = f"规则 #{rule_id} " if rule_id is not None else ""
        console.print(
            f"[yellow]会清掉账号 {account_id} 的{target}回复投递记录"
            "(之后 `--reply-once` 规则可再次触发);加 --yes 确认。[/yellow]"
        )
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            removed = await domain_rules.clear_reply_logs(
                account_id, rule_id=rule_id, actor="cli"
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 已清空 {removed} 条回复投递记录。")

    asyncio.run(_run())
