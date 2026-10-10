"""`xianyu-agent account ...` subcommands: account CRUD (shared with auth)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.mtop.endpoints.user_profile import (
    UserProfileError,
    XianyuUserProfileClient,
)
from xianyu_agent.utils.time_utils import format_local, to_local

app = typer.Typer(help="账号 CRUD(增删改查、启停)。")
console = Console()


@app.command("add")
def add(
    account_id: str = typer.Option(..., "--id", "-i", help="账号标识(自己取名,如 unb)。"),
    nickname: str = typer.Option("", "--nickname", help="昵称,可空。"),
    remark: str = typer.Option("", "--remark", "-r", help="备注,可空。"),
) -> None:
    """新建账号(不含 Cookie;Cookie 通过 `auth login` 录入)。"""

    async def _run() -> None:
        row = await domain_accounts.create_account(
            account_id,
            nickname=nickname or None,
            remark=remark or None,
        )
        console.print(f"[green]OK[/green] 账号 [cyan]{row.account_id}[/cyan] 已创建(id={row.id})。")

    asyncio.run(_run())


@app.command("list")
def list_accounts() -> None:
    """列出所有账号。"""

    async def _run() -> None:
        rows = await domain_accounts.list_accounts()
        if not rows:
            console.print("[dim]暂无账号,先 `account add` 或 `auth login`。[/dim]")
            return
        table = Table(title=f"账号列表 ({len(rows)})")
        table.add_column("account_id", style="cyan")
        table.add_column("nickname")
        table.add_column("remark")
        table.add_column("enabled")
        table.add_column("desired")
        table.add_column("status")
        table.add_column("last_heartbeat_at")
        for r in rows:
            table.add_row(
                r.account_id,
                r.nickname or "-",
                r.remark or "-",
                "Y" if r.enabled else "N",
                r.desired_state,
                r.status,
                format_local(r.last_heartbeat_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_account(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """查看账号详情。"""

    async def _run() -> None:
        r = await domain_accounts.get_account(account_id)
        if r is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"账号 {account_id}")
        table.add_column("字段", style="cyan")
        table.add_column("值")
        for col in (
            "id",
            "account_id",
            "nickname",
            "remark",
            "enabled",
            "desired_state",
            "status",
            "last_login_at",
            "last_heartbeat_at",
            "reply_paused_until",
            "reply_paused_reason",
            "created_at",
        ):
            val = getattr(r, col)
            if isinstance(val, bool):
                val = "Y" if val else "N"
            elif val is not None and hasattr(val, "isoformat"):
                val = to_local(val).isoformat(timespec="seconds")
            table.add_row(col, str(val) if val is not None else "-")
        console.print(table)

    asyncio.run(_run())


@app.command("refresh-profile")
def refresh_profile(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """从平台读取该账号的昵称/头像,并把昵称写进本地镜像(只读接口)。

    需要账号已完成扫码登录且 Cookie 里有签名 token;不修改任何平台状态。
    """

    async def _run() -> None:
        if await domain_accounts.get_account(account_id) is None:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        try:
            profile = await XianyuUserProfileClient().fetch_profile(account_id)
        except UserProfileError as exc:
            console.print(f"[red]读取账号资料失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if profile.nickname:
            await domain_accounts.set_nickname(account_id, profile.nickname)
        console.print(
            f"[green]OK[/green] {account_id} 昵称 {profile.nickname or '-'}"
            + (f",头像 {profile.avatar_url}" if profile.avatar_url else "")
        )
        if profile.nickname is None:
            console.print("[yellow]平台响应里没有昵称字段,本地值未改动。[/yellow]")

    asyncio.run(_run())


@app.command("pause-reply")
def pause_reply(
    account_id: str = typer.Option(..., "--id", "-i"),
    minutes: int = typer.Option(..., "--minutes", "-m", min=1, max=7 * 24 * 60),
    reason: str = typer.Option("", "--reason", "-r", help="暂停原因(只给运营看)。"),
) -> None:
    """暂停该账号的自动回复一段时间;到点自动恢复。

    这是账号级开关,优先于会话状态:风控期间只需停一次,不用逐个会话去关。
    """

    async def _run() -> None:
        until = datetime.now(UTC) + timedelta(minutes=minutes)
        ok = await domain_accounts.set_reply_pause(
            account_id, until=until, reason=reason or None
        )
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[yellow]OK[/yellow] {account_id} 的自动回复已暂停 {minutes} 分钟,到 "
            f"{to_local(until).isoformat(timespec='seconds')} 自动恢复。"
        )

    asyncio.run(_run())


@app.command("resume-reply")
def resume_reply(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """立即恢复该账号的自动回复。"""

    async def _run() -> None:
        ok = await domain_accounts.set_reply_pause(account_id, until=None)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] {account_id} 的自动回复已恢复。")

    asyncio.run(_run())


@app.command("enable")
def enable_account(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """允许账号运行;仍需 `pool start` 才会变为期望在线。"""

    async def _run() -> None:
        ok = await domain_accounts.set_enabled(account_id, True)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[green]OK[/green] {account_id} 已启用;执行 `pool start --account {account_id}` 上线。"
        )

    asyncio.run(_run())


@app.command("disable")
def disable_account(
    account_id: str = typer.Option(..., "--id", "-i"),
) -> None:
    """禁用账号并把 desired_state 置 stopped;daemon 会停止对应 Worker。"""

    async def _run() -> None:
        ok = await domain_accounts.set_enabled(account_id, False)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[yellow]OK[/yellow] {account_id} 已禁用。")

    asyncio.run(_run())


@app.command("delete")
def delete_account(
    account_id: str = typer.Option(..., "--id", "-i"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认。"),
) -> None:
    """删除账号(级联删除 Cookie / 消息 / 订单)。"""
    if not yes:
        confirm = typer.confirm(f"确认删除账号 {account_id}?(级联删除关联数据)")
        if not confirm:
            raise typer.Abort()

    async def _run() -> None:
        ok = await domain_accounts.delete_account(account_id)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[red]OK[/red] {account_id} 已删除。")

    asyncio.run(_run())
