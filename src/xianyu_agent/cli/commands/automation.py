"""平台运营自动化配置;真实协议动作必须先完成校准。"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.automation import (
    PlatformAutomationService,
    enqueue_immediate_run,
)
from xianyu_agent.config import get_settings
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="擦亮与求花自动化(校准门禁、配置、状态)。")
calibration_app = typer.Typer(help="登记真实协议校准结果。")
app.add_typer(calibration_app, name="calibration")
console = Console()


def _fail(exc: Exception) -> None:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1) from exc


@app.command("status")
def status(account_id: str = typer.Option(..., "--account", "-a")) -> None:
    """查看账号的擦亮/求花校准与启用状态。"""

    async def _run() -> None:
        try:
            rows = await PlatformAutomationService().list(account_id)
        except ValueError as exc:
            _fail(exc)
            return
        if not rows:
            console.print("[dim]尚无平台自动化配置;所有动作保持停用。[/dim]")
            return
        table = Table(title=f"平台自动化 ({account_id})")
        for column in (
            "action",
            "enabled",
            "calibrated",
            "approved_at",
            "time",
            "delay_min",
            "quiet_hours",
        ):
            table.add_column(column)
        for row in rows:
            table.add_row(
                row.action,
                "Y" if row.enabled else "N",
                "Y" if row.calibrated else "N",
                format_local(row.calibration_approved_at) or "-",
                row.schedule_time,
                str(row.delay_minutes),
                row.quiet_hours or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("seller-features")
def seller_features(account_id: str = typer.Option(..., "--account", "-a")) -> None:
    """查看账号级卖家能力投影(只读,不修改平台或配置)。"""

    async def _run() -> None:
        try:
            row = await PlatformAutomationService().seller_features(account_id)
        except ValueError as exc:
            _fail(exc)
            return
        table = Table(title=f"卖家能力 ({account_id})")
        for column in ("polish", "request_flower", "auto_rate", "auto_thanks", "unsupported"):
            table.add_column(column)
        table.add_row(
            "Y" if row.polish_enabled else "N",
            "Y" if row.request_flower_enabled else "N",
            "Y" if row.auto_rate_enabled else "N",
            "Y" if row.auto_thanks_enabled else "N",
            ",".join(row.unsupported) or "-",
        )
        console.print(table)

    asyncio.run(_run())


@app.command("run-now")
def run_now(
    account_id: str = typer.Option(..., "--account", "-a"),
    action: str = typer.Option(..., "--action", help="polish 或 request_flower。"),
) -> None:
    """立刻排一次平台动作(不绕过门禁,由 daemon 执行)。

    要求全局 `XIANYU_AUTOMATION_MODE=active`、动作已登记真实校准且已启用,并且当前不在静默时段;
    任务会进同一个持久化队列,因此幂等、租约与审计语义和定时执行完全一致。
    """

    async def _run() -> None:
        settings = get_settings()
        try:
            work_id = await enqueue_immediate_run(
                account_id=account_id,
                action=action,
                active=settings.automation_mode == "active",
                timezone=settings.tz,
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] 已排入队列:{work_id}。daemon 会在下一个调度轮("
            f"默认 30 秒内)执行;用 `automation runs -a {account_id}` 查看结果。"
        )

    asyncio.run(_run())


@app.command("runs")
def runs(
    account_id: str = typer.Option(..., "--account", "-a"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
) -> None:
    """查看擦亮与求花的逐目标执行结果。"""

    async def _run() -> None:
        try:
            rows = await PlatformAutomationService().list_runs(account_id, limit=limit)
        except ValueError as exc:
            _fail(exc)
            return
        if not rows:
            console.print("[dim]尚无平台自动化执行记录。[/dim]")
            return
        table = Table(title=f"平台自动化执行 ({account_id})")
        for column in ("id", "started_at", "action", "target", "status", "result"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                str(row.id),
                format_local(row.started_at) or "-",
                row.action,
                row.target_id,
                row.status,
                row.result or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("resolve")
def resolve_run(
    run_id: int = typer.Option(..., "--run-id", help="要收尾的自动化执行 id。"),
    status: str = typer.Option(..., "--status", help="completed 或 failed。"),
    note: str = typer.Option(..., "--note", help="收尾依据,例如平台订单页已确认发货。"),
    resolved_by: str = typer.Option("operator", "--by", help="收尾人标识。"),
) -> None:
    """人工收尾一条结果不明的自动化执行(不触发任何平台动作)。

    `uncertain` 表示请求可能已经发出:自动流程既不重发也不退库存,只有人工核对平台后才能定性。
    """

    async def _run() -> None:
        try:
            row = await PlatformAutomationService().resolve_run(
                run_id, status=status, note=note, resolved_by=resolved_by
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] 执行 #{row.id} 已收尾为 {row.status}({row.result or '-'})"
        )

    asyncio.run(_run())


@app.command("set")
def configure(  # noqa: PLR0917 - Typer 命令参数就是公开配置面
    account_id: str = typer.Option(..., "--account", "-a"),
    action: str = typer.Option(..., "--action", help="polish 或 request_flower。"),
    enabled: bool = typer.Option(False, "--enabled/--disabled"),
    schedule_time: str = typer.Option("", "--time", help="北京时间 HH:MM。"),
    delay_minutes: int | None = typer.Option(None, "--delay-minutes", min=0, max=43200),
    quiet_hours: str = typer.Option("", "--quiet-hours", help="HH:MM-HH:MM。"),
) -> None:
    """设置自动化;没有真实校准记录时拒绝启用。"""

    async def _run() -> None:
        try:
            row = await PlatformAutomationService().configure(
                account_id,
                action,
                enabled=enabled,
                schedule_time=schedule_time or None,
                delay_minutes=delay_minutes,
                quiet_hours=quiet_hours or None,
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] {row.action} enabled={'Y' if row.enabled else 'N'} "
            f"calibrated={'Y' if row.calibrated else 'N'}"
        )

    asyncio.run(_run())


@calibration_app.command("approve")
def approve_calibration(
    account_id: str = typer.Option(..., "--account", "-a"),
    action: str = typer.Option(..., "--action", help="polish 或 request_flower。"),
    approved_by: str = typer.Option("operator", "--by", help="批准人标识,不得填密钥或账号资料。"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """仅在按真实验证清单确认成功后登记校准通过。"""
    if not yes:
        console.print("[red]请先完成真实校准,再加 --yes 登记批准。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            row = await PlatformAutomationService().approve_calibration(
                account_id, action, approved_by=approved_by
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] {row.action} 已登记真实校准;自动化仍为 disabled。"
        )

    asyncio.run(_run())


@calibration_app.command("revoke")
def revoke_calibration(
    account_id: str = typer.Option(..., "--account", "-a"),
    action: str = typer.Option(..., "--action"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """撤销校准并强制停用对应动作。"""
    if not yes:
        console.print("[red]撤销会同时停用自动化,请加 --yes。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            row = await PlatformAutomationService().revoke_calibration(account_id, action)
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[yellow]OK[/yellow] {row.action} 校准已撤销并停用。")

    asyncio.run(_run())
