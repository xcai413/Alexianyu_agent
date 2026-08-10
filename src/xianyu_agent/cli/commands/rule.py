"""`xianyu-agent rule ...` subcommands: reply rule CRUD + dry-run test."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import rules as domain_rules
from xianyu_agent.domain.rules import DEFAULT_PRIORITY

app = typer.Typer(help="回复规则(关键词 / 正则 / 默认)。")
console = Console()


@app.command("add")
def add_rule(  # noqa: PLR0917 (CLI option surface)
    name: str = typer.Option(..., "--name", "-n", help="规则名。"),
    type_: str = typer.Option("keyword", "--type", "-t", help="keyword | regex | default"),
    pattern: str = typer.Option("", "--pattern", "-p", help="匹配模式(默认规则可留空)。"),
    reply: str = typer.Option(..., "--reply", "-r", help="回复内容。"),
    account_id: str = typer.Option("", "--account", "-a", help="限定账号;留空=全局。"),
    priority: int = typer.Option(DEFAULT_PRIORITY, "--priority", help="越小越优先。"),
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
                priority=priority,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        scope = account_id or "全局"
        console.print(
            f"[green]OK[/green] 规则 #{row.id} [cyan]{row.name}[/cyan] "
            f"({row.type},{scope},priority={row.priority})"
        )

    asyncio.run(_run())


@app.command("list")
def list_rules(
    account_id: str = typer.Option("", "--account", "-a", help="只看某账号的规则。"),
) -> None:
    """列出规则。"""

    async def _run() -> None:
        rows = await domain_rules.list_rules(account_id=account_id or None)
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
        table.add_column("hits")
        for r in rows:
            scope = "全局" if r.account_id is None else str(r.account_id)
            table.add_row(
                str(r.id),
                r.name,
                r.type,
                r.pattern or "-",
                (r.reply_text or "")[:40],
                scope,
                str(r.priority),
                "Y" if r.enabled else "N",
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
) -> None:
    """干跑:显示哪些规则会命中,不发消息。"""

    async def _run() -> None:
        target = account_id or "__global__"
        rows = await domain_rules.match_for_account(target, content)
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
