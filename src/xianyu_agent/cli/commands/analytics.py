"""`xianyu-agent analytics ...`:经营与自动化健康的只读分析。"""

from __future__ import annotations

import asyncio
from datetime import date

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.analytics import AnalyticsService
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="运营分析:窗口指标、自动化健康与镜像水位(只读)。")
console = Console()


def _parse_days(value: str) -> tuple[int, ...]:
    days: list[int] = []
    for part in value.split(","):
        text = part.strip()
        if not text:
            continue
        if not text.isdigit():
            msg = f"--days 只能是逗号分隔的天数: {value!r}"
            raise ValueError(msg)
        days.append(int(text))
    if not days:
        msg = "--days 不能为空"
        raise ValueError(msg)
    return tuple(days)


@app.command("report")
def report(
    account_id: str = typer.Option("", "--account", "-a", help="只看这个账号。"),
    days: str = typer.Option("1,7,30", "--days", help="统计窗口,逗号分隔,例如 1,7,30。"),
    start_date: str = typer.Option("", "--start-date", help="开始日期 YYYY-MM-DD(上海时区)。"),
    end_date: str = typer.Option("", "--end-date", help="结束日期 YYYY-MM-DD,含当天,最多 365 天。"),
) -> None:
    """打印运营分析报告(不发起任何平台请求)。"""

    async def _run() -> None:
        try:
            windows = _parse_days(days)
            result = await AnalyticsService().report(
                account_id=account_id or None,
                windows=windows,
                start_date=date.fromisoformat(start_date) if start_date else None,
                end_date=date.fromisoformat(end_date) if end_date else None,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        table = Table(title=f"运营分析 ({account_id or '全部账号'})")
        for column in (
            "窗口",
            "下单",
            "已付款",
            "已发货",
            "成交额",
            "收到消息",
            "发出消息",
            "新增商品",
        ):
            table.add_column(column)
        for window in result.windows:
            table.add_row(
                f"{window.days} 天",
                str(window.orders),
                str(window.paid_orders),
                str(window.delivered_orders),
                f"{window.amount:.2f}",
                str(window.inbound_messages),
                str(window.outbound_messages),
                str(window.new_items),
            )
        console.print(table)
        if result.range_start and result.range_end:
            console.print(
                f"自定义范围:{result.range_start} 至 {result.range_end}(上海时区,含首尾日期)"
            )
        health = result.automation
        console.print(
            f"自动化:发货完成 {health.jobs_completed} / 失败 {health.jobs_failed} / "
            f"等人处理 {health.jobs_waiting_human} | 平台动作完成 {health.automation_completed} / "
            f"结果不明 {health.automation_uncertain}"
        )
        console.print(
            f"镜像水位:在售 {result.on_sale_items} 件,最近商品同步 "
            f"{format_local(result.last_item_sync_at) or '-'},最近订单同步 "
            f"{format_local(result.last_order_sync_at) or '-'}"
        )
        console.print(f"待人工处理:{result.attention_items} 项(见 `attention list`)")
        console.print("[dim]数字来自本地镜像:如果很久没同步 item/order,这里的值会偏低。[/dim]")

    asyncio.run(_run())
