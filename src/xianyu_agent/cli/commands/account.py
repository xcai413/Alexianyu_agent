"""`xianyu-agent account ...` subcommands: account CRUD (shared with auth)."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import accounts as domain_accounts

app = typer.Typer(help="账号 CRUD(增删改查、启停)。")
console = Console()


@app.command("add")
def add(
    account_id: str = typer.Option(..., "--id", "-i", help="账号标识(自己取名,如 unb)。"),
    nickname: str = typer.Option("", "--nickname", help="昵称,可空。"),
    remark: str = typer.Option("", "--remark", "-r", help="备注,可空。"),
) -> None:
    """新建账号(不含 Cookie;Cookie 通过 `auth login` 录入)。"""

    async def _run() -> None:
        row = await domain_accounts.create_account(
            account_id,
            nickname=nickname or None,
            remark=remark or None,
        )
        console.print(f"[green]OK[/green] 账号 [cyan]{row.account_id}[/cyan] 已创建(id={row.id})。")

    asyncio.run(_run())


@app.command("list")
def list_accounts() -> None:
    """列出所有账号。"""

    async def _run() -> None:
        rows = await domain_accounts.list_accounts()
        if not rows:
            console.print("[dim]暂无账号,先 `account add` 或 `auth login`。[/dim]")
            return
        table = Table(title=f"账号列表 ({len(rows)})")
        table.add_column("account_id", style="cyan")
        table.add_column("nickname")
        table.add_column("remark")
        table.add_column("enabled")
        table.add_column("status")
        table.add_column("last_heartbeat_at")
        for r in rows:
            table.add_row(
                r.account_id,
                r.nickname or "-",
                r.remark or "-",
                "Y" if r.enabled else "N",
                r.status,
                r.last_heartbeat_at.isoformat(timespec="seconds") if r.last_heartbeat_at else "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_account(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """查看账号详情。"""

    async def _run() -> None:
        r = await domain_accounts.get_account(account_id)
        if r is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"账号 {account_id}")
        table.add_column("字段", style="cyan")
        table.add_column("值")
        for col in (
            "id",
            "account_id",
            "nickname",
            "remark",
            "enabled",
            "status",
            "last_login_at",
            "last_heartbeat_at",
            "created_at",
        ):
            val = getattr(r, col)
            if isinstance(val, bool):
                val = "Y" if val else "N"
            elif val is not None and hasattr(val, "isoformat"):
                val = val.isoformat(timespec="seconds")
            table.add_row(col, str(val) if val is not None else "-")
        console.print(table)

    asyncio.run(_run())


@app.command("enable")
def enable_account(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """启用账号(下次 pool start-all 会拉起)。"""

    async def _run() -> None:
        ok = await domain_accounts.set_enabled(account_id, True)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] {account_id} 已启用。")

    asyncio.run(_run())


@app.command("disable")
def disable_account(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """禁用账号(Worker 不会启动)。"""

    async def _run() -> None:
        ok = await domain_accounts.set_enabled(account_id, False)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]OK[/yellow] {account_id} 已禁用。")

    asyncio.run(_run())


@app.command("delete")
def delete_account(
    account_id: str = typer.Option(..., "--id", "-i"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """删除账号(级联删除 Cookie / 消息 / 订单)。"""
    if not yes:
        confirm = typer.confirm(f"确认删除账号 {account_id}?(级联删除关联数据)")
        if not confirm:
            raise typer.Abort()

    async def _run() -> None:
        ok = await domain_accounts.delete_account(account_id)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[red]OK[/red] {account_id} 已删除。")

    asyncio.run(_run())
