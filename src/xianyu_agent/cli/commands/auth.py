"""`xianyu-agent auth ...` subcommands."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from xianyu_agent.db import Account, get_async_session
from xianyu_agent.protocol.signer import CookieSigner

app = typer.Typer(help="管理账号 Cookie 与 token 签名。")
console = Console()


@app.command("login")
def login(
    account_id: str = typer.Option(..., "--account", "-a", help="闲鱼账号 ID(自己取名)。"),
    cookie: str = typer.Option(
        ..., "--cookie", "-c", help="完整 Cookie 字符串(包含 unb / _m_h5_tk / cookie2 等)。"
    ),
    remark: str = typer.Option("", "--remark", "-r", help="备注,可空。"),
) -> None:
    """保存账号 + 加密 Cookie(若账号不存在则新建)。"""

    async def _run() -> None:
        async with get_async_session() as session:
            stmt = select(Account).where(Account.account_id == account_id).limit(1)
            account = (await session.execute(stmt)).scalar_one_or_none()
            if account is None:
                account = Account(
                    account_id=account_id,
                    nickname=None,
                    remark=remark or None,
                    enabled=True,
                )
                session.add(account)
                await session.commit()
                await session.refresh(account)
            elif remark:
                account.remark = remark
                await session.commit()
        signer = CookieSigner()
        ok = await signer.save_cookie(account_id, cookie)
        if not ok:
            console.print(f"[red]保存失败:账号 {account_id} 不存在或 Fernet Key 未配置。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 账号 [cyan]{account_id}[/cyan] Cookie 已加密保存。")

    asyncio.run(_run())


@app.command("status")
def status(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """显示账号 token 状态(脱敏)。"""

    async def _run() -> None:
        signer = CookieSigner()
        fp = await signer.fingerprint(account_id)
        if fp is None:
            console.print(f"[red]未找到账号 {account_id} 或无 Cookie。[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"账号 {account_id}", show_header=False)
        table.add_column("field", style="cyan")
        table.add_column("value")
        for k, v in fp.items():
            table.add_row(k, str(v))
        console.print(table)

    asyncio.run(_run())


@app.command("refresh")
def refresh(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """手动刷新 Cookie(Phase 1 占位:打印当前指纹)。"""

    async def _run() -> None:
        signer = CookieSigner()
        fp = await signer.fingerprint(account_id)
        if fp is None:
            console.print("[red]无 Cookie 可刷新。[/red] 请先 [cyan]auth login[/cyan]。")
            raise typer.Exit(code=1)
        console.print(
            "[yellow]手动刷新未实现。[/yellow] Phase 1 通过重新 [cyan]auth login --cookie <新值>[/cyan] 替换。"
        )
        console.print(json.dumps(fp, ensure_ascii=False, indent=2))

    asyncio.run(_run())


@app.command("list")
def list_accounts() -> None:
    """列出所有账号。"""

    async def _run() -> None:
        async with get_async_session() as session:
            stmt = select(Account).order_by(Account.created_at.desc())
            rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            console.print("[dim]暂无账号,先 [cyan]auth login --account <id>[/cyan]。[/dim]")
            return
        table = Table(title=f"账号列表 ({len(rows)})")
        table.add_column("account_id", style="cyan")
        table.add_column("nickname")
        table.add_column("remark")
        table.add_column("enabled")
        table.add_column("status", style="magenta")
        table.add_column("last_login_at")
        for r in rows:
            table.add_row(
                r.account_id,
                r.nickname or "-",
                r.remark or "-",
                "Y" if r.enabled else "N",
                r.status,
                r.last_login_at.isoformat() if r.last_login_at else "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """查看账号详情(包含字段)。"""

    async def _run() -> None:
        async with get_async_session() as session:
            stmt = select(Account).where(Account.account_id == account_id).limit(1)
            r = (await session.execute(stmt)).scalar_one_or_none()
        if r is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"账号 {account_id}")
        table.add_column("字段", style="cyan")
        table.add_column("值")
        for col in (
            "account_id",
            "nickname",
            "remark",
            "enabled",
            "status",
            "last_login_at",
            "last_heartbeat_at",
            "created_at",
            "updated_at",
        ):
            val = getattr(r, col)
            if isinstance(val, bool):
                val = "Y" if val else "N"
            table.add_row(col, str(val) if val is not None else "-")
        console.print(table)

    asyncio.run(_run())


@app.command("enable")
def enable(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """启用账号。"""

    async def _run() -> None:
        async with get_async_session() as session:
            stmt = select(Account).where(Account.account_id == account_id).limit(1)
            r = (await session.execute(stmt)).scalar_one_or_none()
            if r is None:
                console.print("[red]账号不存在。[/red]")
                raise typer.Exit(code=1)
            r.enabled = True
            await session.commit()
        console.print(f"[green]OK[/green] {account_id} 已启用。")

    asyncio.run(_run())


@app.command("disable")
def disable(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """禁用账号(Worker 不会启动)。"""

    async def _run() -> None:
        async with get_async_session() as session:
            stmt = select(Account).where(Account.account_id == account_id).limit(1)
            r = (await session.execute(stmt)).scalar_one_or_none()
            if r is None:
                console.print("[red]账号不存在。[/red]")
                raise typer.Exit(code=1)
            r.enabled = False
            await session.commit()
        console.print(f"[yellow]OK[/yellow] {account_id} 已禁用。")

    asyncio.run(_run())


@app.command("delete")
def delete(
    account_id: str = typer.Option(..., "--account", "-a"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """删除账号及其 Cookie / 消息 / 订单(级联)。"""
    if not yes:
        confirm = typer.confirm(f"确认删除账号 {account_id}?(级联删除其 Cookie / 消息 / 订单)")
        if not confirm:
            raise typer.Abort()

    async def _run() -> None:
        async with get_async_session() as session:
            stmt = select(Account).where(Account.account_id == account_id).limit(1)
            r = (await session.execute(stmt)).scalar_one_or_none()
            if r is None:
                console.print("[red]账号不存在。[/red]")
                raise typer.Exit(code=1)
            await session.delete(r)
            await session.commit()
        console.print(f"[red]OK[/red] {account_id} 已删除。")

    asyncio.run(_run())
