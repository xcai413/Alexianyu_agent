"""`xianyu-agent auth ...` subcommands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import qrcode as qrcode_lib
import qrcode.constants
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.qr_login import QRLoginClient, QrStatus
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.utils.time_utils import format_local

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
                format_local(r.last_login_at) or "-",
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


@app.command("qr-login")
def qr_login(
    account_id: str = typer.Option(..., "--account", "-a", help="登录后保存到的账号标识。"),
    timeout: float = typer.Option(300.0, "--timeout", help="等待扫码总时长(秒)。"),
    qr_out: str = typer.Option(
        "", "--qr-out", help="二维码 PNG 保存路径;默认 data/qr_logins/<session>.png。"
    ),
    remark: str = typer.Option("", "--remark", "-r", help="账号备注(可选)。"),
) -> None:
    """扫码登录闲鱼账号:生成二维码 -> 手机扫码确认 -> 自动保存 Cookie。"""

    async def _run() -> None:
        client = QRLoginClient()
        console.print("[dim]正在生成二维码...[/dim]")
        try:
            session = await client.generate()
        except Exception as exc:
            console.print(f"[red]生成二维码失败: {exc}[/red]")
            raise typer.Exit(code=1) from exc

        # 渲染二维码:PNG + 终端 ASCII
        try:
            qr = qrcode_lib.QRCode(
                version=5,
                error_correction=qrcode_lib.constants.ERROR_CORRECT_L,
                box_size=8,
                border=2,
            )
            qr.add_data(session.qr_content or "")
            qr.make()
            qr.print_ascii(invert=True)
            out_path = (
                Path(qr_out)
                if qr_out
                else (get_settings().data_dir / "qr_logins" / f"{session.session_id}.png")
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            qr.make_image().save(out_path)
            console.print(f"\n[green]二维码已保存:[/green] {out_path}")
        except Exception as exc:
            console.print(f"[yellow]二维码渲染失败(仍可继续): {exc}[/yellow]")

        console.print(
            f"请用闲鱼 App 扫码(会话 {session.session_id[:8]}...),等待 {timeout:.0f} 秒..."
        )

        last_status = session.status
        status_map = {
            QrStatus.SCANNED: "已扫码,请在手机上确认",
            QrStatus.SUCCESS: "登录成功",
            QrStatus.EXPIRED: "二维码已过期",
            QrStatus.CANCELLED: "已取消",
            QrStatus.VERIFICATION_REQUIRED: "需要手机验证",
        }

        def on_status(status: str) -> None:
            nonlocal last_status
            if status != last_status and status in status_map:
                console.print(f"[cyan]{status_map[status]}[/cyan]")
                last_status = status

        final = await client.wait_for_login(session, timeout_s=timeout, on_status=on_status)

        if final == QrStatus.SUCCESS and session.unb:
            # 建账号(幂等)并保存加密 Cookie
            await domain_accounts.create_account(account_id, remark=remark or None)
            saved = await CookieSigner().save_cookie(account_id, session.cookie_string())
            if not saved:
                console.print("[red]Cookie 保存失败。[/red]")
                raise typer.Exit(code=1)
            console.print(
                f"[green]OK[/green] 扫码登录成功,账号 [cyan]{account_id}[/cyan] "
                f"(unb={session.unb}),Cookie 已加密保存。"
            )
        elif final == QrStatus.VERIFICATION_REQUIRED:
            console.print(
                f"[red]账号被风控,需要手机验证:[/red] {session.verification_url or '未知'}"
            )
            raise typer.Exit(code=2)
        else:
            console.print(f"[red]扫码未完成: {final}[/red]")
            raise typer.Exit(code=1)

    asyncio.run(_run())
