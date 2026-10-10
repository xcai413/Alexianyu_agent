"""Web console provisioning commands that never persist a plaintext password."""

from __future__ import annotations

import typer
from rich.console import Console

from xianyu_agent.interfaces.web.auth.security import hash_password

app = typer.Typer(help="Web 运营后台的本地初始化工具。", no_args_is_help=True)
console = Console()


@app.command("password-hash")
def password_hash() -> None:
    """交互读取管理员密码并打印可写入 .env 的 scrypt 哈希。"""
    password = typer.prompt("管理员密码", hide_input=True, confirmation_prompt=True)
    # Compose 会展开未加单引号的 $salt/$digest;输出能被两种 dotenv 读取器原样保存。
    console.print(f"XIANYU_WEB_ADMIN_PASSWORD_HASH='{hash_password(password)}'", soft_wrap=True)
    console.print("[dim]请将上行写入受保护的 .env,不要提交到 Git。[/dim]")
