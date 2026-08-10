"""`xianyu-agent order ...` subcommands."""

from __future__ import annotations

import asyncio
import contextlib

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, get_async_session
from xianyu_agent.domain import orders as domain_orders
from xianyu_agent.protocol.client import WsClient
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.services.delivery_service import DeliveryService
from xianyu_agent.services.guardrails import Guardrails
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
        async with get_async_session() as session:
            account_row = (
                await session.execute(
                    select(Account).where(Account.id == r.account_id).limit(1)
                )
            ).scalar_one_or_none()
        account_key = account_row.account_id if account_row is not None else str(r.account_id)
        console.rule(f"订单 #{r.id}")
        console.print(f"order_id:   {r.order_id}")
        console.print(f"account:    {account_key}")
        console.print(f"item:       {r.item_title or '-'} ({r.item_id or '-'})")
        console.print(f"buyer:      {r.buyer_name or '-'} ({r.buyer_id or '-'})")
        console.print(f"amount:     {r.amount}")
        console.print(f"status:     {r.status}")
        console.print(f"paid_at:    {format_local(r.paid_at) or '-'}")
        console.print(f"delivered:  {format_local(r.delivered_at) or '-'}")
        console.print(f"发货内容:   [cyan]{r.delivery_content or '(未发货)'}[/cyan]")
        console.print(f"失败原因:   {r.delivery_fail_reason or '-'}")

    asyncio.run(_run())


@app.command("redeliver")
def redeliver_order(order_id: int = typer.Argument(...)) -> None:
    """重试发货(发送失败过的订单,复用已预留的卡密)。"""

    async def _run() -> None:
        r = await domain_orders.get_by_id(order_id)
        if r is None:
            console.print(f"[red]订单 #{order_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        if r.status != "paid":
            console.print(
                f"[yellow]订单状态 {r.status} 不是 paid,无需重试发货。[/yellow]"
            )
            raise typer.Exit(code=1)
        async with get_async_session() as session:
            account_row = (
                await session.execute(
                    select(Account).where(Account.id == r.account_id).limit(1)
                )
            ).scalar_one_or_none()
        account_key = account_row.account_id if account_row is not None else str(r.account_id)
        if not get_settings().ws_url:
            console.print(
                "[red]未配置 XIANYU_WS_URL,无法真实发送。[/red]"
                "\n请先配置 WS URL 后重试,或在线运行 worker(pool start)。"
            )
            raise typer.Exit(code=1)

        connected = asyncio.Event()

        async def _on_state(state) -> None:
            if state.state == ConnectionState.CONNECTED:
                connected.set()

        client = WsClient(account_key, on_state=_on_state)
        client.start()
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(connected.wait(), timeout=10.0)
            if client.state != ConnectionState.CONNECTED:
                console.print(
                    f"[red]WS 连接未就绪(state={client.state.value}),重试取消。[/red]"
                )
                raise typer.Exit(code=1)

            async def sender(_a: str, _o: str, content: str) -> bool:
                return await client.send_text(content)

            svc = DeliveryService(sender=sender, guardrails=Guardrails())
            result = await svc.retry(order_id)
        finally:
            await client.stop()
        if result.delivered:
            console.print(
                f"[green]重试成功:[/green] 订单 {result.order_id} 已发货 "
                f"(code={result.code or '-'})。"
            )
        else:
            console.print(
                f"[red]重试失败:[/red] {result.order_id} -> {result.reason}"
            )
            raise typer.Exit(code=1)

    asyncio.run(_run())
