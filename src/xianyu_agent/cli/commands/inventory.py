"""xianyu-agent inventory 子命令:库存池、卡密单元与预留。

这里是库存的人工与 Agent 入口:池定义"从哪发货",单元是逐行卡密,预留把单元锁给某个订单。
默认只显示卡密掩码预览;要看明文必须显式加 --reveal。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.delivery import SpecSourceService
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryQueryService,
    InventoryReservationService,
    InventoryService,
)
from xianyu_agent.domain.inventory import (
    POOL_TYPES,
    UNIT_STATES,
    InvalidStateTransition,
    Reservation,
)
from xianyu_agent.domain.inventory.reservation import (
    ConcurrentReservationError,
    InsufficientStock,
)
from xianyu_agent.domain.item.sku import normalize_specs

app = typer.Typer(help="库存池与卡密单元(原子预留;预留才意味着这张卡归某个订单)。")
pool_app = typer.Typer(help="库存池:text / data / image / api。")
unit_app = typer.Typer(help="卡密单元:逐行导入、查看状态。")
reservation_app = typer.Typer(help="预留:查看、退回、结果未知的处理。")
app.add_typer(pool_app, name="pool")
app.add_typer(unit_app, name="unit")
app.add_typer(reservation_app, name="reservation")
spec_app = typer.Typer(help="多规格发货:按买家选中的规格组合找库存来源。")
app.add_typer(spec_app, name="spec")
console = Console()


def _fail(message: object) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


@pool_app.command("create")
def create_pool(  # noqa: PLR0917 (CLI option surface)
    name: str = typer.Option(..., "--name", "-n", help="池名称。"),
    type_: str = typer.Option("data", "--type", "-t", help=f"池类型:{' | '.join(POOL_TYPES)}"),
    delay_seconds: int = typer.Option(0, "--delay", min=0, max=3600, help="发货前延迟秒数。"),
    account_scope: str = typer.Option("", "--account", "-a", help="限定账号(可选)。"),
    item_scope: str = typer.Option("", "--item", help="限定商品 id(可选)。"),
    sku_scope: str = typer.Option("", "--sku", help="限定规格键(可选)。"),
    description: str = typer.Option("", "--desc", help="描述(可选)。"),
    api_config_ref: str = typer.Option("", "--api-config", help="api 池的配置引用(不含密钥)。"),
    content: str = typer.Option("", "--content", "-c", help="text 池的发货文案。"),
    image_url: str = typer.Option("", "--image-url", help="image 池的图片地址。"),
    enabled: bool = typer.Option(True, "--enabled/--disabled"),
) -> None:
    """新建库存池(data 池才持有可消费的卡密单元)。"""

    async def _run() -> None:
        try:
            pool = await InventoryService().create_pool(
                name=name,
                type_=type_,
                description=description or None,
                enabled=enabled,
                delay_seconds=delay_seconds,
                account_scope=account_scope or None,
                item_scope=item_scope or None,
                sku_scope=sku_scope or None,
                api_config_ref=api_config_ref or None,
                content=content or None,
                image_url=image_url or None,
            )
        except ValueError as exc:
            _fail(exc)
        console.print(f"[green]OK[/green] {pool.describe()} 延迟 {pool.delay_seconds}s")

    asyncio.run(_run())


@pool_app.command("list")
def list_pools(
    only_enabled: bool = typer.Option(False, "--enabled", help="只看启用的池。"),
    type_: str = typer.Option("", "--type", "-t", help="只看某个类型。"),
) -> None:
    """列出库存池与余量。"""

    async def _run() -> None:
        try:
            views = await InventoryQueryService().list_pools(
                only_enabled=only_enabled, type_=type_ or None
            )
        except ValueError as exc:
            _fail(exc)
        if not views:
            console.print("[dim]暂无库存池,先用 inventory pool create 建一个。[/dim]")
            return
        table = Table(title=f"库存池 ({len(views)})")
        for column in (
            "id",
            "name",
            "type",
            "scope",
            "delay",
            "available",
            "reserved",
            "uncertain",
            "enabled",
        ):
            table.add_column(column)
        for view in views:
            pool = view.pool
            scope = "/".join(
                part for part in (pool.account_scope, pool.item_scope, pool.sku_scope) if part
            )
            table.add_row(
                str(pool.local_id),
                pool.name,
                pool.type,
                scope or "-",
                f"{pool.delay_seconds}s",
                str(view.available),
                str(view.reserved),
                str(view.uncertain),
                "Y" if pool.enabled else "N",
            )
        console.print(table)

    asyncio.run(_run())


@pool_app.command("show")
def show_pool(pool_id: int = typer.Argument(...)) -> None:
    """查看一个库存池的详情与余量。"""

    async def _run() -> None:
        try:
            view = await InventoryQueryService().pool_detail(pool_id)
        except ValueError as exc:
            _fail(exc)
        pool = view.pool
        console.print(f"[cyan]{pool.describe()}[/cyan]")
        console.print(
            f"类型 {pool.type} | 延迟 {pool.delay_seconds}s | 启用 {pool.enabled} | "
            f"余量 available={view.available} reserved={view.reserved} "
            f"consumed={view.counts.get('consumed', 0)} "
            f"uncertain={view.uncertain} total={view.total}"
        )
        if pool.description:
            console.print(f"说明:{pool.description}")
        if pool.api_config_ref:
            console.print(f"API 配置引用:{pool.api_config_ref}")
        if pool.content:
            console.print(f"发货文案:{pool.content}")
        if pool.image_url:
            console.print(f"图片地址:{pool.image_url}")

    asyncio.run(_run())


@pool_app.command("content")
def set_pool_content(
    pool_id: int = typer.Argument(...),
    content: str = typer.Option("", "--content", "-c", help="text 池的发货文案。"),
    image_url: str = typer.Option("", "--image-url", help="image 池的图片地址。"),
) -> None:
    """设置策略型池的内容(text 文案 / image 地址)。"""

    async def _run() -> None:
        if not content and not image_url:
            _fail("请用 --content 或 --image-url 提供内容")
            return
        try:
            pool = await InventoryService().update_pool(
                pool_id,
                content=content or None,
                image_url=image_url or None,
            )
        except ValueError as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] {pool.describe()}")

    asyncio.run(_run())


@pool_app.command("enable")
def enable_pool(pool_id: int = typer.Argument(...)) -> None:
    """启用库存池。"""

    async def _run() -> None:
        try:
            pool = await InventoryService().set_enabled(pool_id, enabled=True)
        except ValueError as exc:
            _fail(exc)
        console.print(f"[green]OK[/green] {pool.describe()} 已启用。")

    asyncio.run(_run())


@pool_app.command("disable")
def disable_pool(pool_id: int = typer.Argument(...)) -> None:
    """停用库存池(已存在的预留不受影响)。"""

    async def _run() -> None:
        try:
            pool = await InventoryService().set_enabled(pool_id, enabled=False)
        except ValueError as exc:
            _fail(exc)
        console.print(f"[yellow]OK[/yellow] {pool.describe()} 已停用。")

    asyncio.run(_run())


@pool_app.command("delay")
def set_pool_delay(
    pool_id: int = typer.Argument(...),
    seconds: int = typer.Option(..., "--seconds", min=0, max=3600, help="发货前延迟秒数。"),
) -> None:
    """调整发货延迟(0 表示立即)。"""

    async def _run() -> None:
        try:
            pool = await InventoryService().update_pool(pool_id, delay_seconds=seconds)
        except ValueError as exc:
            _fail(exc)
        console.print(f"[green]OK[/green] 库存池 #{pool.local_id} 延迟 {pool.delay_seconds}s")

    asyncio.run(_run())


@pool_app.command("set")
def set_pool(  # noqa: PLR0917 (CLI option surface)
    pool_id: int = typer.Argument(...),
    name: str = typer.Option("", "--name", help="新名称;留空不改。"),
    description: str = typer.Option("", "--desc", help="新描述;留空不改。"),
    delay_seconds: int | None = typer.Option(
        None, "--delay", min=0, max=3600, help="新的发货前延迟秒数。"
    ),
    content: str = typer.Option("", "--content", help="新的 text 池文案;留空不改。"),
    image_url: str = typer.Option("", "--image-url", help="新的 image 池地址;留空不改。"),
    account_scope: str = typer.Option("", "--account", help="新的账号范围;留空不改。"),
    item_scope: str = typer.Option("", "--item", help="新的商品范围;留空不改。"),
    sku_scope: str = typer.Option("", "--sku", help="新的规格范围;留空不改。"),
    clear_scope: bool = typer.Option(
        False, "--clear-scope", help="清除账号/商品/规格范围限制。"
    ),
) -> None:
    """修改库存池定义;类型与 API 引用保持不变。"""
    if not any(
        (
            name,
            description,
            delay_seconds is not None,
            content,
            image_url,
            account_scope,
            item_scope,
            sku_scope,
            clear_scope,
        )
    ):
        _fail("至少提供一项修改内容")

    async def _run() -> None:
        try:
            pool = await InventoryService().update_pool(
                pool_id,
                name=name or None,
                description=description or None,
                delay_seconds=delay_seconds,
                content=content or None,
                image_url=image_url or None,
                account_scope=account_scope or None,
                item_scope=item_scope or None,
                sku_scope=sku_scope or None,
                clear_scope=clear_scope,
            )
        except ValueError as exc:
            _fail(exc)
        console.print(f"[green]OK[/green] {pool.describe()} 已更新。")

    asyncio.run(_run())


@pool_app.command("delete")
def delete_pool(
    pool_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """删除库存池(连同单元与预留记录)。"""
    if not yes and not typer.confirm(f"确认删除库存池 #{pool_id} 及其全部单元?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            await InventoryService().delete_pool(pool_id)
        except ValueError as exc:
            _fail(exc)
        console.print(f"[red]OK[/red] 库存池 #{pool_id} 已删除。")

    asyncio.run(_run())


@unit_app.command("import")
def import_units(
    pool_id: int = typer.Argument(...),
    file: Path = typer.Option(  # noqa: B008 - Typer 选项必须在默认值处构造
        None, "--file", "-f", help="UTF-8 文本文件,一行一条卡密。"
    ),
    content: str = typer.Option("", "--content", "-c", help="直接给出卡密,一行一条。"),
) -> None:
    """导入逐行卡密;重复行与空白行不会被计入。"""

    async def _run() -> None:
        service = InventoryImportService()
        try:
            if file is not None:
                summary = await service.import_file(pool_id, file)
            elif content:
                summary = await service.import_units(pool_id, content.splitlines())
            else:
                _fail("请用 --file 或 --content 提供卡密")
                return
        except ValueError as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] 池 #{pool_id} 新增 {summary.added} 条,"
            f"跳过重复 {summary.skipped_duplicates} 条、空白 {summary.skipped_blank} 行。"
        )

    asyncio.run(_run())


@unit_app.command("list")
def list_units(
    pool_id: int = typer.Argument(...),
    state: str = typer.Option("", "--state", help=f"只看某个状态:{' | '.join(UNIT_STATES)}"),
    limit: int = typer.Option(50, "--limit", min=1, max=1000),
    reveal: bool = typer.Option(False, "--reveal", help="显示明文卡密(默认只显示掩码)。"),
) -> None:
    """查看库存单元。"""

    async def _run() -> None:
        try:
            units = await InventoryQueryService().list_units(
                pool_id, state=state or None, limit=limit
            )
        except ValueError as exc:
            _fail(exc)
            return
        if not units:
            console.print("[dim]暂无单元。[/dim]")
            return
        table = Table(title=f"池 #{pool_id} 单元 ({len(units)})")
        for column in ("id", "state", "preview", "reserved_by", "reservation"):
            table.add_column(column)
        for unit in units:
            table.add_row(
                str(unit.unit_id),
                unit.state,
                unit.preview,
                unit.reserved_by or "-",
                str(unit.reservation_id or "-"),
            )
        console.print(table)
        if reveal:
            contents = await InventoryQueryService().plain_contents(
                pool_id, state=state or None, limit=limit
            )
            console.print("[yellow]明文卡密(仅人工核对用,勿粘贴到外部):[/yellow]")
            for line in contents:
                console.print(line)

    asyncio.run(_run())


def _render_reservations(reservations: list[Reservation]) -> None:
    if not reservations:
        console.print("[dim]暂无预留。[/dim]")
        return
    table = Table(title=f"预留 ({len(reservations)})")
    for column in ("id", "pool", "job", "quantity", "state", "units"):
        table.add_column(column)
    for reservation in reservations:
        table.add_row(
            str(reservation.local_id),
            str(reservation.pool_id),
            reservation.job_ref,
            str(reservation.quantity),
            reservation.state,
            ",".join(str(unit_id) for unit_id in reservation.unit_ids) or "-",
        )
    console.print(table)


@reservation_app.command("list")
def list_reservations(
    job: str = typer.Option("", "--job", help="按占用方过滤(通常是 delivery job)。"),
    state: str = typer.Option("", "--state", help="按状态过滤。"),
) -> None:
    """列出预留记录。"""

    async def _run() -> None:
        reservations = await InventoryQueryService().list_reservations(
            job_ref=job or None, state=state or None
        )
        _render_reservations(reservations)

    asyncio.run(_run())


@reservation_app.command("show")
def show_reservation(reservation_id: int = typer.Argument(...)) -> None:
    """查看一条预留。"""

    async def _run() -> None:
        try:
            reservation = await InventoryReservationService().get(reservation_id)
        except ValueError as exc:
            _fail(exc)
            return
        _render_reservations([reservation])

    asyncio.run(_run())


@reservation_app.command("reserve")
def reserve_units(
    pool_id: int = typer.Option(..., "--pool", "-p", help="从哪个库存池取。"),
    job_ref: str = typer.Option(
        ..., "--job", "-j", help="占用方标识(delivery job 或人工补发单号)。"
    ),
    quantity: int = typer.Option(1, "--quantity", "-q", min=1, max=100),
    reveal: bool = typer.Option(False, "--reveal", help="显示明文卡密(默认只显示掩码)。"),
) -> None:
    """人工预留卡密(补发、测试与离线履约用)。"""

    async def _run() -> None:
        service = InventoryReservationService()
        try:
            reservation = await service.reserve(pool_id, job_ref, quantity)
        except (ValueError, InsufficientStock, ConcurrentReservationError) as exc:
            _fail(exc)
            return
        _render_reservations([reservation])
        if reveal and reservation.local_id is not None:
            contents = await service.contents(reservation.local_id)
            console.print("[yellow]明文卡密(仅人工核对用,勿粘贴到外部):[/yellow]")
            for line in contents:
                console.print(line)

    asyncio.run(_run())


@reservation_app.command("release")
def release_reservation(
    reservation_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """退回预留:只在明确没有发出卡密时使用。"""
    if not yes and not typer.confirm(f"确认退回预留 #{reservation_id} 的卡密?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            released = await InventoryReservationService().release(reservation_id)
        except (ValueError, InvalidStateTransition) as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] 预留 #{reservation_id} 退回 {released} 条,已回到可用。")

    asyncio.run(_run())


@reservation_app.command("consume")
def consume_reservation(
    reservation_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """核销预留:卡密确实已经交到买家手里(人工发送后补记)。"""
    if not yes and not typer.confirm(f"确认预留 #{reservation_id} 已成功发出并核销?"):
        raise typer.Abort()

    async def _run() -> None:
        try:
            consumed = await InventoryReservationService().consume(reservation_id)
        except (ValueError, InvalidStateTransition) as exc:
            _fail(exc)
            return
        console.print(f"[green]OK[/green] 预留 #{reservation_id} 已核销 {consumed} 条。")

    asyncio.run(_run())


@reservation_app.command("uncertain")
def mark_reservation_uncertain(reservation_id: int = typer.Argument(...)) -> None:
    """标记结果未知:外部可能已经收到,库存冻结,等人工或对账决定。"""

    async def _run() -> None:
        try:
            frozen = await InventoryReservationService().mark_uncertain(reservation_id)
        except (ValueError, InvalidStateTransition) as exc:
            _fail(exc)
            return
        console.print(
            f"[yellow]OK[/yellow] 预留 #{reservation_id} 已冻结 {frozen} 条,"
            "确认后再用 resolve --delivered/--not-delivered 收尾。"
        )

    asyncio.run(_run())


@reservation_app.command("resolve")
def resolve_reservation(
    reservation_id: int = typer.Argument(...),
    delivered: bool = typer.Option(
        ..., "--delivered/--not-delivered", help="外部到底有没有收到这张卡。"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """处理结果未知的预留:确认已发就核销,确认没发就退回。"""
    if not yes:
        action = "核销" if delivered else "退回"
        if not typer.confirm(f"确认{action}预留 #{reservation_id}?"):
            raise typer.Abort()

    async def _run() -> None:
        try:
            resolved = await InventoryReservationService().resolve_uncertain(
                reservation_id, delivered=delivered
            )
        except (ValueError, InvalidStateTransition) as exc:
            _fail(exc)
            return
        console.print(
            f"[green]OK[/green] 预留 #{reservation_id} 已按"
            f"{'已发送' if delivered else '未发送'}处理 {resolved} 条。"
        )

    asyncio.run(_run())


def _parse_specs(values: Sequence[str]) -> list[tuple[str, str]]:
    """把重复出现的 规格名=规格值 选项解析成规格对。"""
    pairs: list[tuple[str, str]] = []
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name.strip() or not value.strip():
            msg = f"--spec 需要 规格名=规格值 形式: {item}"
            raise ValueError(msg)
        pairs.append((name.strip(), value.strip()))
    if not pairs:
        msg = "至少需要一个 --spec"
        raise ValueError(msg)
    return pairs


@spec_app.command("plan")
def plan_spec(
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id", help="平台商品 ID。"),
    spec: list[str] = typer.Option(..., "--spec", help="规格,形如 颜色=红色,可重复"),  # noqa: B008
) -> None:
    """Dry Run:看这个规格组合会从哪儿发货、发几份。"""

    async def _run() -> None:
        try:
            plan = await SpecSourceService().plan(
                account_id, item_id, normalize_specs(_parse_specs(spec))
            )
        except ValueError as exc:
            _fail(exc)
            return
        if plan.matched:
            console.print(
                f"[green]命中[/green] {item_id} {plan.selection_key} -> {plan.describe()}"
            )
        else:
            console.print(f"[yellow]未命中[/yellow] {item_id}: {plan.reason}")

    asyncio.run(_run())


@spec_app.command("reserve")
def reserve_spec(
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id", help="平台商品 ID。"),
    spec: list[str] = typer.Option(..., "--spec", help="规格,形如 颜色=红色,可重复"),  # noqa: B008
    job_ref: str = typer.Option(..., "--job", "-j", help="占用方标识(delivery job 或补发单号)。"),
    reveal: bool = typer.Option(False, "--reveal", help="显示明文卡密(默认只显示掩码)。"),
) -> None:
    """按规格组合预留库存(补发与离线履约用)。"""

    async def _run() -> None:
        service = SpecSourceService()
        try:
            reservation = await service.reserve(
                account_id, item_id, normalize_specs(_parse_specs(spec)), job_ref=job_ref
            )
        except (ValueError, InsufficientStock, ConcurrentReservationError) as exc:
            _fail(exc)
            return
        _render_reservations([reservation])
        if reveal and reservation.local_id is not None:
            contents = await InventoryReservationService().contents(reservation.local_id)
            console.print("[yellow]明文卡密(仅人工核对用,勿粘贴到外部):[/yellow]")
            for line in contents:
                console.print(line)

    asyncio.run(_run())
