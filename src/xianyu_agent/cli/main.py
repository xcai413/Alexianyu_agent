"""xianyu-agent CLI entry.

Single source of truth for all agent operations.
Subcommand groups:
  - auth      Cookie / token 管理 (login/status/refresh/enable/disable/...)
  - account   账号 CRUD (Phase 2 扩展: account list 等)
  - message   消息查询 (Phase 1 起可用)
  - order     订单查询 (Phase 4 起)
  - rule      回复规则 CRUD (Phase 3 起)
  - card      卡密库存 CRUD (Phase 4 起)
  - protocol   协议层命令 (connect/inject/watch)
  - pool      账号池管理 (Phase 2 起)
  - daemon    常驻运行与进程级控制 (P0.1)
  - dashboard Textual 驾驶舱 (Phase 5 起)
  - mcp       MCP Server (Phase 6 起)
"""

from __future__ import annotations

import sys
from typing import NoReturn

import typer
from rich.console import Console

from xianyu_agent import __version__
from xianyu_agent.cli.commands import (
    account as account_cmd,
    auth as auth_cmd,
    card as card_cmd,
    daemon as daemon_cmd,
    dashboard as dashboard_cmd,
    item as item_cmd,
    maintenance as maintenance_cmd,
    mcp as mcp_cmd,
    message as message_cmd,
    order as order_cmd,
    pool as pool_cmd,
    protocol as protocol_cmd,
    rule as rule_cmd,
)
from xianyu_agent.config import ensure_fernet_key

app = typer.Typer(
    name="xianyu-agent",
    help="为 AI Agent 直接控制的闲鱼运营系统。",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
app.add_typer(auth_cmd.app, name="auth")
app.add_typer(account_cmd.app, name="account")
app.add_typer(card_cmd.app, name="card")
app.add_typer(dashboard_cmd.app, name="dashboard")
app.add_typer(daemon_cmd.app, name="daemon")
app.add_typer(item_cmd.app, name="item")
app.add_typer(maintenance_cmd.app, name="maintenance")
app.add_typer(mcp_cmd.app, name="mcp")
app.add_typer(message_cmd.app, name="message")
app.add_typer(order_cmd.app, name="order")
app.add_typer(pool_cmd.app, name="pool")
app.add_typer(protocol_cmd.app, name="protocol")
app.add_typer(rule_cmd.app, name="rule")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold cyan]xianyu-agent[/bold cyan] [dim]v{__version__}[/dim]")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="输出版本号后退出。",
    ),
) -> None:
    """xianyu-agent 全局选项。"""
    _ = version  # typer 选项占位(实际处理在 _version_callback)
    try:
        ensure_fernet_key()
    except Exception as exc:
        console.print(f"[yellow]自动生成 FERNET_KEY 失败:{exc}[/yellow]")


@app.command()
def hello() -> None:
    """冒烟命令。"""
    console.print(f"[green]你好![/green] 这是 xianyu-agent [bold]v{__version__}[/bold]。")
    console.print(
        "Phase 0-8 已交付。常用入口:[cyan]pool status[/cyan] / [cyan]dashboard[/cyan] / "
        "[cyan]mcp serve[/cyan] / [cyan]rule test[/cyan]。真实联调需配置 XIANYU_WS_URL + Cookie。"
    )


def run() -> NoReturn:
    """脚本入口(`python -m xianyu_agent.cli.main`)。"""
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    run()
