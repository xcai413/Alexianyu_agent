"""`xianyu-agent mcp ...` subcommands: MCP Server (SSE over FastAPI)."""

from __future__ import annotations

import typer
import uvicorn
from fastapi_mcp import FastApiMCP
from rich.console import Console

from xianyu_agent.api.app import app as fastapi_app
from xianyu_agent.config import get_settings

app = typer.Typer(help="MCP Server(给 Agent 调领域能力)。")
console = Console()


@app.command("serve")
def serve(
    port: int = typer.Option(0, "--port", "-p", help="监听端口;0=用 .env XIANYU_MCP_PORT。"),
    host: str = typer.Option("", "--host", help="监听地址;空=用 .env XIANYU_MCP_HOST。"),
) -> None:
    """启动 MCP Server(HTTP + SSE,fastapi-mcp 自动生成 tools)。"""
    mcp = FastApiMCP(
        fastapi_app,
        name="xianyu-agent",
        description="闲鱼运营 Agent API:账号/消息/规则/卡密/订单/池",
        describe_all_responses=False,
    )
    mcp.mount_sse()

    settings = get_settings()
    listen_port = port or settings.mcp_port
    listen_host = host or settings.mcp_host
    console.print(
        f"[green]MCP Server[/green] http://{listen_host}:{listen_port}/mcp "
        f"(SSE);tools 由 {len(fastapi_app.routes)} 条路由生成"
    )
    uvicorn.run(fastapi_app, host=listen_host, port=listen_port, log_level="warning")
