"""`xianyu-agent attention ...`:需要人来决定的待办清单。

清单只读。它把发货任务、平台自动化执行与账号风控里"卡住等人"的事实聚到一起,并附上收尾命令,
避免运营在多个子命令之间来回找。
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.exc import OperationalError

from xianyu_agent.application.attention import AttentionItem, AttentionService
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="待人工处理:结果不明、需要确认或需要重新验证的事项。")
console = Console()

KIND_LABELS = {
    "delivery_job": "发货任务",
    "automation_run": "平台自动化",
    "account_risk": "账号风控",
}


def _render(items: list[AttentionItem]) -> None:
    if not items:
        console.print("[green]当前没有需要人工处理的事项。[/green]")
        return
    table = Table(title=f"待人工处理 ({len(items)})")
    for column in ("类型", "对象", "账号", "状态", "原因", "时间"):
        table.add_column(column, overflow="fold")
    for item in items:
        table.add_row(
            KIND_LABELS.get(item.kind, item.kind),
            item.target,
            item.account_id or "-",
            item.state,
            item.reason,
            format_local(item.since) or "-",
        )
    console.print(table)
    console.print("[dim]下一步命令:[/dim]")
    for item in items:
        console.print(f"  [cyan]{item.next_command}[/cyan]")


@app.command("list")
def list_attention(
    account_id: str = typer.Option("", "--account", "-a", help="只看这个账号。"),
    limit: int = typer.Option(200, "--limit", min=1, max=1000),
) -> None:
    """列出所有需要人工处理的事项。"""

    async def _run() -> None:
        try:
            items = await AttentionService().list_items(
                account_id=account_id or None, limit=limit
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        except OperationalError as exc:
            # 本地库没升到最新迁移时,清单依赖的表可能还不存在;这里给可执行的下一步,
            # 而不是把 SQL 报错栈直接丢给运营。
            console.print(
                "[red]数据库结构与当前代码不匹配[/red];请先运行 "
                "[cyan]uv run alembic upgrade head[/cyan]。"
            )
            raise typer.Exit(code=1) from exc
        _render(items)

    asyncio.run(_run())
