"""`xianyu-agent card ...` subcommands: card inventory."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain.inventory import cards as domain_cards

app = typer.Typer(help="卡密库存(文本/数据/图片/API)。")
console = Console()


@app.command("add")
def add_card(  # noqa: PLR0917 (CLI option surface)
    account_id: str = typer.Option(..., "--account", "-a"),
    name: str = typer.Option(..., "--name", "-n"),
    content: str = typer.Option(
        "", "--content", "-c", help="文本卡密一行一个;其他类型为单个内容。"
    ),
    type_: str = typer.Option("text", "--type", "-t", help="text | data | image | api"),
    unit_price: float = typer.Option(0.0, "--price", help="单价(可选)。"),
    description: str = typer.Option("", "--desc", help="描述(可选)。"),
) -> None:
    """新建卡密(文本卡自动按行计数)。"""

    async def _run() -> None:
        try:
            row = await domain_cards.create_card(
                account_id,
                name,
                content or None,
                type_=type_,
                unit_price=unit_price,
                description=description or None,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]OK[/green] 卡 #{row.id} [cyan]{row.name}[/cyan] "
            f"({row.type}, total={row.total}, remaining={row.remaining})"
        )

    asyncio.run(_run())


@app.command("list")
def list_cards(
    account_id: str = typer.Option("", "--account", "-a"),
    only_enabled: bool = typer.Option(False, "--enabled", help="只看启用的。"),
) -> None:
    """列出卡密库存。"""

    async def _run() -> None:
        rows = await domain_cards.list_cards(
            account_id=account_id or None, only_enabled=only_enabled
        )
        if not rows:
            console.print("[dim]暂无卡密,先 `card add`。[/dim]")
            return
        table = Table(title=f"卡密库存 ({len(rows)})")
        table.add_column("id")
        table.add_column("name", style="cyan")
        table.add_column("account")
        table.add_column("type")
        table.add_column("total")
        table.add_column("remaining", style="magenta")
        table.add_column("price")
        table.add_column("enabled")
        for r in rows:
            table.add_row(
                str(r.id),
                r.name,
                str(r.account_id),
                r.type,
                str(r.total),
                str(r.remaining),
                str(r.unit_price),
                "Y" if r.enabled else "N",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("restock")
def restock_card(
    card_id: int = typer.Argument(...),
    content: str = typer.Option(..., "--content", "-c", help="新增卡密,一行一个。"),
) -> None:
    """追加卡密库存。"""

    async def _run() -> None:
        row = await domain_cards.restock(card_id, content)
        if row is None:
            console.print(f"[red]卡 #{card_id} 不存在或内容为空。[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[green]OK[/green] 卡 #{card_id} 现在 total={row.total} remaining={row.remaining}"
        )

    asyncio.run(_run())


@app.command("consume")
def consume_card(
    card_id: int = typer.Argument(...),
) -> None:
    """手动取一张卡密(测试 / 补发用)。"""

    async def _run() -> None:
        code = await domain_cards.consume_card(card_id, None)
        if code is None:
            console.print(f"[red]卡 #{card_id} 无可用卡密。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 取出: [cyan]{code[:40]}[/cyan]")

    asyncio.run(_run())


@app.command("enable")
def enable_card(card_id: int = typer.Argument(...)) -> None:
    async def _run() -> None:
        ok = await domain_cards.set_card_enabled(card_id, True)
        if not ok:
            console.print(f"[red]卡 #{card_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 卡 #{card_id} 已启用。")

    asyncio.run(_run())


@app.command("disable")
def disable_card(card_id: int = typer.Argument(...)) -> None:
    async def _run() -> None:
        ok = await domain_cards.set_card_enabled(card_id, False)
        if not ok:
            console.print(f"[red]卡 #{card_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]OK[/yellow] 卡 #{card_id} 已禁用。")

    asyncio.run(_run())


@app.command("delete")
def delete_card(
    card_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除卡密(级联删除消费记录)。"""
    if not yes:
        confirm = typer.confirm(f"确认删除卡 #{card_id}?")
        if not confirm:
            raise typer.Abort()

    async def _run() -> None:
        ok = await domain_cards.delete_card(card_id)
        if not ok:
            console.print(f"[red]卡 #{card_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[red]OK[/red] 卡 #{card_id} 已删除。")

    asyncio.run(_run())
