"""`xianyu-agent notify ...`:通知渠道(对照 Helper 的 notifications/*)。

渠道只往运营自己的地址推消息,不碰闲鱼平台;密钥只以 SecretVault 引用出入,列表与详情都不回显
地址正文(Cookie 之外,机器人 webhook 里的 token 同样不能进日志)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from xianyu_agent.application.notify import NotificationChannelService
from xianyu_agent.application.ports.secret_vault import (
    SecretMetadata,
    SecretRef,
    SecretValue,
)
from xianyu_agent.db import AlertDispatch, get_async_session
from xianyu_agent.domain.notify import (
    CHANNEL_KINDS,
    NOTIFY_EVENTS,
    NotificationChannel,
    event_label,
)
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="通知渠道:把待人工处理事项推到钉钉/飞书/Telegram/Bark/Webhook。")
console = Console()

channel_app = typer.Typer(help="渠道的增删改查与试发送。")
secret_app = typer.Typer(help="渠道密钥:加密写入 SecretVault,永不回显明文。")
app.add_typer(channel_app, name="channel")
app.add_typer(secret_app, name="secret")


def _vault() -> SqlAlchemySecretVault:
    return SqlAlchemySecretVault(FernetKeyring.from_environment())


@secret_app.command("add")
def add_secret(
    value: str = typer.Option(..., prompt="密钥值", hide_input=True),
    label: str = typer.Option("", "--label", help="非敏感用途标签。"),
) -> None:
    """写入一个渠道密钥,输出可放进 --secret-ref 的不透明引用。"""

    async def _run() -> None:
        try:
            ref = await _vault().put(
                SecretValue(value),
                metadata=SecretMetadata(
                    {"purpose": "notification", "label": label.strip() or "notification"}
                ),
            )
        except Exception as exc:
            console.print(f"[red]保存渠道密钥失败:{type(exc).__name__}。[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 密钥已加密保存;引用:[cyan]{ref.value}[/cyan]")

    asyncio.run(_run())


@secret_app.command("status")
def secret_status(ref: str = typer.Argument(..., help="SecretVault 不透明引用。")) -> None:
    """确认引用是否存在,不读取或显示密钥。"""

    async def _run() -> None:
        configured = await _vault().configured(SecretRef(ref))
        state = "存在" if configured else "不存在"
        console.print(f"引用 {ref}:[cyan]{state}[/cyan]")

    asyncio.run(_run())


def _render(
    channels: Sequence[NotificationChannel], *, accounts: dict[int, list[str]] | None = None
) -> None:
    if not channels:
        console.print("[dim]还没有通知渠道;先 [cyan]notify channel add[/cyan]。[/dim]")
        return
    table = Table(title=f"通知渠道 ({len(channels)})")
    for column in ("id", "名称", "类型", "目标(掩码)", "事件", "密钥", "启用", "绑定账号"):
        table.add_column(column, overflow="fold")
    for channel in channels:
        bound = accounts.get(int(channel.local_id or 0), []) if accounts else []
        table.add_row(
            str(channel.local_id),
            channel.name,
            channel.kind,
            channel.masked_target(),
            "、".join(event_label(event) for event in channel.events) or "全部",
            channel.secret_ref or "-",
            "Y" if channel.enabled else "N",
            "、".join(bound) or ("全部" if not bound else "-"),
        )
    console.print(table)


@channel_app.command("add")
def add_channel(  # noqa: PLR0917 (CLI option surface)
    name: str = typer.Option(..., "--name", "-n", help="渠道名称(唯一)。"),
    kind: str = typer.Option(..., "--kind", "-k", help=f"类型:{'、'.join(CHANNEL_KINDS)}"),
    target: str = typer.Option(..., "--target", "-t", help="https 地址;telegram 填 chat_id。"),
    secret_ref: str = typer.Option("", "--secret-ref", help="SecretVault 引用。"),
    events: str = typer.Option(
        "", "--events", help=f"订阅事件,逗号分隔;留空=全部({'、'.join(NOTIFY_EVENTS)})"
    ),
    description: str = typer.Option("", "--description"),
) -> None:
    """新增一条通知渠道(默认启用)。"""

    async def _run() -> None:
        try:
            channel = await NotificationChannelService().create(
                name=name,
                kind=kind.strip().lower(),
                target=target,
                secret_ref=secret_ref or None,
                events=_parse_events(events),
                description=description or None,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] {channel.describe()}")

    asyncio.run(_run())


def _parse_events(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@channel_app.command("list")
def list_channels(
    only_enabled: bool = typer.Option(False, "--enabled", help="只看启用的渠道。"),
) -> None:
    """列出全部渠道(地址只回掩码)。"""

    async def _run() -> None:
        service = NotificationChannelService()
        try:
            channels = await service.list_channels(only_enabled=only_enabled)
            accounts = {
                int(channel.local_id or 0): await service.bound_accounts(int(channel.local_id or 0))
                for channel in channels
            }
        except OperationalError as exc:
            console.print(
                "[red]数据库结构与当前代码不匹配[/red];请先运行 "
                "[cyan]uv run alembic upgrade head[/cyan]。"
            )
            raise typer.Exit(code=1) from exc
        _render(channels, accounts=accounts)

    asyncio.run(_run())


@channel_app.command("show")
def show_channel(ref: str = typer.Argument(..., help="渠道 id 或名称。")) -> None:
    """查看单条渠道(仍只回掩码)。"""

    async def _run() -> None:
        service = NotificationChannelService()
        try:
            channel = await service.resolve_ref(ref)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if channel.local_id is not None:
            _render(
                [channel],
                accounts={
                    int(channel.local_id): await service.bound_accounts(int(channel.local_id))
                },
            )
        if channel.description:
            console.print(f"说明:{channel.description}")

    asyncio.run(_run())


@channel_app.command("set")
def set_channel(
    channel_id: int = typer.Argument(...),
    target: str = typer.Option("", "--target", help="新地址;留空不改。"),
    secret_ref: str = typer.Option("", "--secret-ref", help="新密钥引用;留空不改。"),
    events: str = typer.Option("", "--events", help="订阅事件;留空不改。"),
    description: str = typer.Option("", "--description", help="说明;留空不改。"),
) -> None:
    """改地址、密钥引用、订阅事件或说明。"""

    async def _run() -> None:
        try:
            channel = await NotificationChannelService().update(
                channel_id,
                target=target or None,
                secret_ref=secret_ref or None,
                events=_parse_events(events) if events else None,
                description=description or None,
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] {channel.describe()}")

    asyncio.run(_run())


@channel_app.command("enable")
def enable_channel(channel_id: int = typer.Argument(...)) -> None:
    """启用一条渠道。"""
    _set_enabled(channel_id, True)


@channel_app.command("disable")
def disable_channel(channel_id: int = typer.Argument(...)) -> None:
    """停用一条渠道(停止发送,不删除配置)。"""
    _set_enabled(channel_id, False)


def _set_enabled(channel_id: int, enabled: bool) -> None:
    async def _run() -> None:
        try:
            channel = await NotificationChannelService().set_enabled(channel_id, enabled=enabled)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] {channel.describe()}")

    asyncio.run(_run())


@channel_app.command("delete")
def delete_channel(
    channel_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除。"),
) -> None:
    """删除渠道(同时删掉绑定);发送历史保留。"""
    if not yes:
        console.print("[yellow]删除会一并删掉账号绑定;确认后加 --yes。[/yellow]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            await NotificationChannelService().delete(channel_id)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 通知渠道 #{channel_id} 已删除。")

    asyncio.run(_run())


@channel_app.command("bind")
def bind_channel(
    channel_id: int = typer.Argument(...),
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """把渠道绑定到账号:绑过账号的渠道只收这些账号的事件。"""

    async def _run() -> None:
        ok = await NotificationChannelService().bind(channel_id, account_id)
        if not ok:
            console.print(f"[red]账号 {account_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 渠道 #{channel_id} 已绑定 {account_id}。")

    asyncio.run(_run())


@channel_app.command("unbind")
def unbind_channel(
    channel_id: int = typer.Argument(...),
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """解除绑定;解到一条不剩时该渠道重新收全部事件。"""

    async def _run() -> None:
        removed = await NotificationChannelService().unbind(channel_id, account_id)
        if not removed:
            console.print("[yellow]没有这条绑定。[/yellow]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 渠道 #{channel_id} 已解绑 {account_id}。")

    asyncio.run(_run())


@channel_app.command("test")
def send_channel_test(channel_id: int = typer.Argument(...)) -> None:
    """发一条测试消息;只回成功或失败原因,不回地址与密钥。"""

    async def _run() -> None:
        try:
            result = await NotificationChannelService().test_send(channel_id)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if result.ok:
            console.print("[green]OK[/green] 测试消息已发出;没收到请检查渠道配置。")
            return
        console.print(f"[red]发送失败:{result.error or '未知原因'}[/red]")
        raise typer.Exit(code=1)

    asyncio.run(_run())


@app.command("messages")
def list_messages(
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    only_failed: bool = typer.Option(False, "--failed", help="只看失败的发送。"),
) -> None:
    """最近的发送事实:每条事项每个渠道只发一次,发送失败也记在这里。"""

    async def _run() -> None:
        async with get_async_session() as session:
            statement = select(AlertDispatch).order_by(AlertDispatch.id.desc()).limit(limit)
            if only_failed:
                statement = statement.where(AlertDispatch.ok.is_(False))
            rows = (await session.scalars(statement)).all()
        if not rows:
            console.print("[dim]还没有通知发送记录。[/dim]")
            return
        table = Table(title=f"通知发送记录 ({len(rows)})")
        for column in ("id", "渠道", "事件", "对象", "状态", "结果", "错误", "时间"):
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(
                str(row.id),
                str(row.channel_id or "env-webhook"),
                event_label(row.kind),
                row.target,
                row.state,
                "OK" if row.ok else "FAIL",
                row.error or "-",
                format_local(row.created_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())
