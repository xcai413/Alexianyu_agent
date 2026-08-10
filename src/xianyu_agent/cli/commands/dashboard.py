"""`xianyu-agent dashboard` 入口:启动 Textual 驾驶舱。"""

from __future__ import annotations

import typer
from rich.console import Console

from xianyu_agent.tui.app import DashboardApp

app = typer.Typer(
    help="终端驾驶舱(实时看板)。",
    no_args_is_help=False,
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def run(
    refresh: float = typer.Option(2.0, "--refresh", "-r", help="刷新间隔(秒)。"),
) -> None:
    """启动 Textual 驾驶舱;按 q 退出,! 紧急模式。"""
    if refresh <= 0:
        Console().print("[red]--refresh 必须 > 0[/red]")
        raise typer.Exit(code=1)
    DashboardApp().run()
