"""`xianyu-agent order ...` subcommands."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import orders as domain_orders
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="订单查询。")
console = Console()


@app.command("list")
def list_orders(
    account_id: str = typer.Option(..., "--account", "-a"),
    status: str = typer.Option("", "--status", "-s", help="过滤状态,如 paid / delivered。"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
) -> None:
    """列出某账号订单。"""

    async def _run() -> None:
        rows = await domain_orders.list_for_account(account_id, status=status or None, limit=limit)
        if not rows:
            console.print(f"[dim]账号 {account_id} 无订单。[/dim]")
            return
        table = Table(title=f"订单 ({account_id})")
        table.add_column("id")
        table.add_column("order_id")
        table.add_column("item", overflow="fold")
        table.add_column("amount")
        table.add_column("status", style="magenta")
        table.add_column("paid_at")
        table.add_column("delivered_at")
        for r in rows:
            table.add_row(
                str(r.id),
                r.order_id,
                (r.item_title or "-")[:30],
                str(r.amount),
                r.status,
                format_local(r.paid_at) or "-",
                format_local(r.delivered_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_order(order_id: int = typer.Argument(...)) -> None:
    """显示订单详情(含发货内容 / 失败原因)。"""

    async def _run() -> None:
        r = await domain_orders.get_by_id(order_id)
        if r is None:
            console.print(f"[red]订单 #{order_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.rule(f"订单 #{r.id}")
        console.print(f"order_id:   {r.order_id}")
        console.print(f"account:    {r.account_id}")
        console.print(f"item:       {r.item_title or '-'} ({r.item_id or '-'})")
        console.print(f"buyer:      {r.buyer_name or '-'} ({r.buyer_id or '-'})")
        console.print(f"amount:     {r.amount}")
        console.print(f"status:     {r.status}")
        console.print(f"paid_at:    {format_local(r.paid_at) or '-'}")
        console.print(f"delivered:  {format_local(r.delivered_at) or '-'}")
        console.print(f"发货内容:   [cyan]{r.delivery_content or '(未发货)'}[/cyan]")
        console.print(f"失败原因:   {r.delivery_fail_reason or '-'}")

    asyncio.run(_run())
