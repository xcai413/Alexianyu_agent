"""`xianyu-agent item ...` 子命令: 在售商品只读镜像。"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import items as domain_items, orders as domain_orders
from xianyu_agent.protocol.items_client import ItemSyncError, XianyuItemsClient
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="在售商品镜像(只读同步,不修改闲鱼商品)。")
console = Console()


@app.command("sync")
def sync_items(
    account_id: str = typer.Option(..., "--account", "-a", help="已扫码登录的账号标识。"),
    page_size: int = typer.Option(20, "--page-size", min=1, max=50),
    max_pages: int = typer.Option(100, "--max-pages", min=1, max=100),
) -> None:
    """只读同步指定账号全部“在售”商品到本地镜像。"""

    async def _run() -> None:
        try:
            snapshot = await XianyuItemsClient().fetch_all_on_sale(
                account_id, page_size=page_size, max_pages=max_pages
            )
            result = await domain_items.apply_on_sale_snapshot(
                account_id, snapshot.items, synced_at=snapshot.fetched_at
            )
        except (ItemSyncError, ValueError) as exc:
            console.print(f"[red]同步失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]OK[/green] {account_id} 在售商品镜像已同步: "
            f"共 {result.total} 条 / {snapshot.pages} 页, "
            f"新增 {result.created}, 更新 {result.updated}, "
            f"标记非在售 {result.marked_off_sale}。"
        )

    asyncio.run(_run())


@app.command("list")
def list_items(
    account_id: str = typer.Option(..., "--account", "-a"),
    include_off_sale: bool = typer.Option(False, "--all", help="包含本地已标记非在售的历史商品。"),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=500),
) -> None:
    """列出本地镜像商品; 先运行 item sync 才会有数据。"""

    async def _run() -> None:
        rows = await domain_items.list_items(
            account_id, on_sale_only=not include_off_sale, limit=limit
        )
        if not rows:
            console.print(
                f"[dim]账号 {account_id} 暂无商品镜像; 先运行 "
                f"`item sync --account {account_id}`。[/dim]"
            )
            return
        table = Table(title=f"商品镜像 ({account_id}, {len(rows)})")
        table.add_column("id")
        table.add_column("item_id", style="cyan")
        table.add_column("标题", overflow="fold")
        table.add_column("价格")
        table.add_column("已售")
        table.add_column("在售")
        table.add_column("最后同步")
        sales = await domain_orders.sales_by_item(account_id)
        for row in rows:
            sale = sales.get(row.item_id)
            table.add_row(
                str(row.id),
                row.item_id,
                row.title,
                row.price or "-",
                str(sale.sold_quantity) if sale is not None else "0",
                "Y" if row.is_on_sale else "N",
                format_local(row.last_synced_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_item(item_pk: int = typer.Argument(...)) -> None:
    """查看一个本地商品镜像详情(不向闲鱼端发请求)。"""

    async def _run() -> None:
        row = await domain_items.get_item(item_pk)
        if row is None:
            console.print(f"[red]商品镜像 #{item_pk} 不存在。[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"商品镜像 #{row.id}", show_header=False)
        table.add_column("字段", style="cyan")
        table.add_column("值")
        for name in (
            "item_id",
            "title",
            "price",
            "status",
            "is_on_sale",
            "detail_url",
            "main_image_url",
            "category_id",
            "auction_type",
            "first_seen_at",
            "last_synced_at",
        ):
            value = getattr(row, name)
            if name in {"first_seen_at", "last_synced_at"}:
                value = format_local(value)
            table.add_row(name, str(value) if value is not None else "-")
        console.print(table)

    asyncio.run(_run())
