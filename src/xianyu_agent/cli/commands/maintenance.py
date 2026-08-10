"""`xianyu-agent maintenance ...` subcommands: housekeeping."""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from xianyu_agent.services.heartbeat import purge_old_messages

app = typer.Typer(help="维护任务(消息清理等)。")
console = Console()


@app.command("purge-messages")
def purge_messages(
    older_than: float = typer.Option(24.0, "--older-than", help="保留窗口(小时)。"),
) -> None:
    """删除早于保留窗口的消息记录。"""

    async def _run() -> None:
        deleted = await purge_old_messages(older_than_hours=older_than)
        console.print(f"[green]OK[/green] 清理了 {deleted} 条过期消息。")

    asyncio.run(_run())
