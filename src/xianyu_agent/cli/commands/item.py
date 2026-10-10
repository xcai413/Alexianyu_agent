"""`xianyu-agent item ...` 子命令: 在售商品只读镜像。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import Sequence
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from xianyu_agent.application.item import (
    ItemBatchService,
    ItemCategoryService,
    ItemDetailService,
    ItemMaterialService,
    ItemPublishService,
    ItemQueryService,
    ItemSyncService,
)
from xianyu_agent.application.item.importers import (
    ImportResult,
    parse_csv,
    parse_tsv,
    parse_xlsx,
    parse_zip_bundle,
)
from xianyu_agent.application.item.lifecycle_service import (
    ItemLifecycleService,
    lifecycle_item_filter,
    lifecycle_policy,
)
from xianyu_agent.application.item.publish_runner import ItemBatchRunner
from xianyu_agent.domain.item import items as domain_items
from xianyu_agent.domain.item.entity import Item
from xianyu_agent.domain.item.publish_job import (
    INTERRUPTED_ERROR,
    BatchPreview,
    BatchPublishJob,
)
from xianyu_agent.domain.item.sku import normalize_specs
from xianyu_agent.domain.item.sku_binding import (
    CARD_TARGET,
    SpecBinding,
    list_item_bindings,
    remove_item_binding,
    upsert_item_binding,
)
from xianyu_agent.domain.order import orders as domain_orders
from xianyu_agent.protocol.items_client import ItemSyncError
from xianyu_agent.protocol.mtop.endpoints.item_category import ItemCategoryError
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailError
from xianyu_agent.protocol.mtop.endpoints.item_polish import (
    ItemPolishError,
    XianyuItemPolishClient,
)
from xianyu_agent.protocol.mtop.endpoints.item_publish import XianyuItemWriter
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="在售商品镜像(只读同步,不修改闲鱼商品)。")
batch_app = typer.Typer(help="批量铺货批次:导入、预览与恢复(发布执行器未接入)。")
lifecycle_app = typer.Typer(help="商品生命周期:Dry Run 与显式确认后的动作。")
automation_app = typer.Typer(help="商品级自动化:单品人工锁定与排除。")
material_app = typer.Typer(help="素材库:本地图片索引(按账号隔离,按内容去重)。")
app.add_typer(batch_app, name="batch")
app.add_typer(lifecycle_app, name="lifecycle")
app.add_typer(automation_app, name="automation")
app.add_typer(material_app, name="material")
console = Console()


@app.command("sync")
def sync_items(
    account_id: str = typer.Option(..., "--account", "-a", help="已扫码登录的账号标识。"),
    page_size: int = typer.Option(20, "--page-size", min=1, max=50),
    max_pages: int = typer.Option(100, "--max-pages", min=1, max=100),
) -> None:
    """只读同步指定账号全部“在售”商品到本地镜像。"""

    async def _run() -> None:
        try:
            outcome = await ItemSyncService().sync_on_sale(
                account_id, page_size=page_size, max_pages=max_pages
            )
        except (ItemSyncError, ValueError) as exc:
            console.print(f"[red]同步失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        result = outcome.result
        console.print(
            f"[green]OK[/green] {account_id} 在售商品镜像已同步: "
            f"共 {result.total} 条 / {outcome.pages} 页, "
            f"新增 {result.created}, 更新 {result.updated}, "
            f"标记非在售 {result.marked_off_sale}, "
            f"刷新已售 {outcome.sold_counts_updated}。"
        )

    asyncio.run(_run())


@app.command("polish")
def polish_item(
    account_id: str = typer.Option(..., "--account", "-a", help="已扫码登录的账号标识。"),
    item_id: str = typer.Option(..., "--item-id", help="要擦亮的商品 id。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行真实擦亮。"),
) -> None:
    """擦亮一个商品(真实平台动作)。

    这是单商品校准入口,不是每日批量任务:协议事实来自两个参考项目的一致观察,
    但尚未在真实账号上校准。每天每件商品的次数由平台限制,重复调用返回「已经擦亮」。
    """
    if not yes:
        console.print("[red]擦亮会向平台发出真实请求,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            result = await XianyuItemPolishClient().polish_item(account_id, item_id)
        except (ItemPolishError, ValueError) as exc:
            console.print(f"[red]擦亮失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if result.already_polished:
            console.print(f"[yellow]OK[/yellow] 商品 {result.item_id} 今天已经擦亮过(幂等跳过)。")
            return
        console.print(f"[green]OK[/green] 商品 {result.item_id} 擦亮成功。")

    asyncio.run(_run())


@app.command("list")
def list_items(
    account_id: str = typer.Option(..., "--account", "-a"),
    include_off_sale: bool = typer.Option(False, "--all", help="包含本地已标记非在售的历史商品。"),
    limit: int = typer.Option(100, "--limit", "-n", min=1, max=500),
) -> None:
    """列出本地镜像商品; 先运行 item sync 才会有数据。"""

    async def _run() -> None:
        items = await ItemQueryService().list_items(
            account_id, on_sale_only=not include_off_sale, limit=limit
        )
        if not items:
            console.print(
                f"[dim]账号 {account_id} 暂无商品镜像; 先运行 "
                f"`item sync --account {account_id}`。[/dim]"
            )
            return
        table = Table(title=f"商品镜像 ({account_id}, {len(items)})")
        table.add_column("id")
        table.add_column("item_id", style="cyan")
        table.add_column("标题", overflow="fold")
        table.add_column("价格")
        table.add_column("已售")
        table.add_column("在售")
        table.add_column("最后同步")
        sales = await domain_orders.sales_by_item(account_id)
        for item in items:
            sale = sales.get(item.external_item_id)
            table.add_row(
                str(item.local_id),
                item.external_item_id,
                item.title,
                "-" if item.price is None else str(item.price),
                str(sale.sold_quantity) if sale is not None else "0",
                "Y" if item.is_on_sale else "N",
                format_local(item.synced_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_item(item_pk: int = typer.Argument(...)) -> None:
    """查看一个本地商品镜像详情(不向闲鱼端发请求)。"""

    async def _run() -> None:
        item = await ItemQueryService().get_item_by_local_id(item_pk)
        if item is None:
            console.print(f"[red]商品镜像 #{item_pk} 不存在。[/red]")
            raise typer.Exit(code=1)
        table = Table(title=f"商品镜像 #{item_pk}", show_header=False)
        table.add_column("字段", style="cyan")
        table.add_column("值")
        for name, value in _detail_rows(item):
            table.add_row(name, value)
        console.print(table)
        if item.variants:
            console.print(_variant_table(item))

    asyncio.run(_run())


@app.command("set")
def set_item(  # noqa: PLR0917 - CLI option surface
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id"),
    title: str = typer.Option("", "--title"),
    description: str | None = typer.Option(None, "--description"),
    price: str | None = typer.Option(None, "--price"),
    category_id: str | None = typer.Option(None, "--category-id"),
    detail_url: str | None = typer.Option(None, "--detail-url"),
    stock: int | None = typer.Option(None, "--stock", min=0),
) -> None:
    """修改本地商品目录字段;不会调用闲鱼商品编辑接口。"""
    if not any((title, description is not None, price is not None, category_id is not None, detail_url is not None, stock is not None)):
        console.print("[red]至少提供一项商品字段修改。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            item = await domain_items.update_local_item(
                account_id,
                item_id,
                title=title or None,
                description=description,
                price=price,
                category_id=category_id,
                detail_url=detail_url,
                stock=stock,
            )
        except ValueError as exc:
            console.print(f"[red]本地商品修改失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if item is None:
            console.print(f"[red]商品 {item_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 本地商品 {item_id} 已更新;未调用平台接口。")

    asyncio.run(_run())


@app.command("delete")
def delete_item(
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认归档本地商品。"),
) -> None:
    """归档本地商品目录记录;不删除平台商品和订单历史。"""
    if not yes:
        console.print("[red]这只会归档本地商品,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        if not await domain_items.archive_local_item(account_id, item_id):
            console.print(f"[red]商品 {item_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]OK[/green] 本地商品 {item_id} 已归档;未调用平台接口。")

    asyncio.run(_run())


@app.command("category")
def recommend_category(
    account_id: str = typer.Option(..., "--account", "-a"),
    keyword: str = typer.Option(..., "--keyword", "-k", help="商品标题或关键词"),
) -> None:
    """按关键词推荐可发布的类目(按参考实现构造,尚未真实校准)。"""

    async def _run() -> None:
        try:
            category = await ItemCategoryService().recommend(account_id, keyword=keyword)
        except (ItemCategoryError, ValueError) as exc:
            console.print(f"[red]类目推荐失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"推荐类目: {category.describe()}")
        console.print(
            f"[dim]发布可用字段: cat_id={category.cat_id} / "
            f"channel_cat_id={category.channel_cat_id} / "
            f"tb_cat_id={category.tb_cat_id or '-'}[/dim]"
        )

    asyncio.run(_run())


@app.command("detail")
def sync_item_detail(
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id", help="平台商品 ID"),
    structure: bool = typer.Option(
        False, "--structure", help="只打印响应结构(键名/类型/长度),用于校准字段路径。"
    ),
) -> None:
    """读取单个商品的平台详情(按参考实现构造,尚未真实校准)。"""

    async def _run() -> None:
        if structure:
            try:
                dump = await ItemDetailService().fetch_structure(account_id, item_id)
            except (ItemDetailError, ValueError) as exc:
                console.print(f"[red]详情结构抓取失败:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            console.print_json(data=json.loads(json.dumps(dump, ensure_ascii=False)))
            console.print(
                "[dim]以上只有键名、类型与列表长度,不含任何取值;可用于校准字段路径。[/dim]"
            )
            return
        try:
            facts = await ItemDetailService().sync_facts(account_id, item_id)
        except (ItemDetailError, ValueError) as exc:
            console.print(f"[red]详情读取失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if facts is None:
            console.print(f"[red]商品 {item_id} 不在本地镜像里,请先运行 item sync。[/red]")
            raise typer.Exit(code=1)
        multi_spec = "Y" if facts.multi_spec else "N"
        publisher = "已识别" if facts.publisher_id else "未识别"
        console.print(f"商品 {item_id}: 多规格={multi_spec} / 发布人={publisher}")
        console.print("[dim]详情里的标题、描述与图片字段路径尚未校准,本次只记录多规格标记。[/dim]")

    asyncio.run(_run())


@app.command("bind")
def bind_spec(  # noqa: PLR0917 - Typer 命令参数本身就是关键字参数
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id", help="平台商品 ID"),
    spec: list[str] = typer.Option(..., "--spec", help="规格,形如 颜色=红色,可重复"),  # noqa: B008
    target: str = typer.Option(..., "--target", help="库存来源,形如 pool:3 或 card:7"),
    quantity: int = typer.Option(1, "--quantity", min=1, help="每次发货取几份"),
    disabled: bool = typer.Option(False, "--disabled", help="先配置但不启用。"),
) -> None:
    """把某个规格组合绑定到库存来源(pool:<库存池id> 为 Phase 6 自动发货路径)。"""

    async def _run() -> None:
        try:
            kind, target_id = _parse_target(target)
            binding = SpecBinding(
                specs=normalize_specs(_parse_specs(spec)),
                target_kind=kind,
                target_id=target_id,
                quantity=quantity,
                enabled=not disabled,
            )
            bindings = await upsert_item_binding(account_id, item_id, binding)
        except ValueError as exc:
            console.print(f"[red]绑定失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 已绑定 {binding.describe()} -> {target}")
        _print_bindings(bindings)

    asyncio.run(_run())


@app.command("bindings")
def show_bindings(
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id", help="平台商品 ID"),
) -> None:
    """列出某个商品的规格库存绑定。"""

    async def _run() -> None:
        bindings = await list_item_bindings(account_id, item_id)
        if not bindings:
            console.print(f"[dim]商品 {item_id} 尚未配置规格库存绑定。[/dim]")
            return
        _print_bindings(bindings)

    asyncio.run(_run())


@app.command("unbind")
def unbind_spec(
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id", help="平台商品 ID"),
    spec: list[str] = typer.Option(..., "--spec", help="规格,形如 颜色=红色,可重复"),  # noqa: B008
) -> None:
    """解除某个规格组合的绑定。"""

    async def _run() -> None:
        try:
            bindings = await remove_item_binding(
                account_id, item_id, normalize_specs(_parse_specs(spec))
            )
        except ValueError as exc:
            console.print(f"[red]解除绑定失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 已解除绑定,剩余 {len(bindings)} 条。")
        if bindings:
            _print_bindings(bindings)

    asyncio.run(_run())


def _parse_specs(values: Sequence[str]) -> list[tuple[str, str]]:
    """Parse repeated 规格名=规格值 options into spec pairs."""
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


def _parse_target(value: str) -> tuple[str, int]:
    """Parse 类型:id (for example card:7) into a target reference."""
    kind, separator, raw_id = value.partition(":")
    if not separator:
        kind, raw_id = CARD_TARGET, value
    try:
        target_id = int(raw_id.strip())
    except ValueError as exc:
        msg = f"--target 需要 类型:id 形式(例如 card:7): {value}"
        raise ValueError(msg) from exc
    return (kind.strip().lower() or CARD_TARGET), target_id


def _print_bindings(bindings: Sequence[SpecBinding]) -> None:
    table = Table(title=f"规格库存绑定 ({len(bindings)})")
    for column in ("规格", "来源", "每次数量", "启用"):
        table.add_column(column)
    for binding in bindings:
        table.add_row(
            binding.describe(),
            f"{binding.target_kind}:{binding.target_id}",
            str(binding.quantity),
            "Y" if binding.enabled else "N",
        )
    console.print(table)


@material_app.command("import")
def import_material(
    account_id: str = typer.Option(..., "--account", "-a"),
    file: Path = typer.Option(  # noqa: B008 - Typer 选项必须在默认值处构造
        ..., "--file", "-f", exists=True, dir_okay=False, readable=True
    ),
    name: str = typer.Option("", "--name", help="素材名;留空用文件名"),
    tag: list[str] | None = typer.Option(None, "--tag"),  # noqa: B008 - Typer 选项
) -> None:
    """把一张本地图片登记进素材库;相同内容不会重复登记。"""

    async def _run() -> None:
        try:
            material = await ItemMaterialService().import_file(
                account_id, file, name=name or None, tags=tag or ()
            )
        except ValueError as exc:
            console.print(f"[red]素材导入失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]OK[/green] 素材 #{material.local_id} {material.name} "
            f"({material.size_bytes} 字节)"
        )

    asyncio.run(_run())


@material_app.command("list")
def list_material(
    account_id: str = typer.Option(..., "--account", "-a"),
    include_disabled: bool = typer.Option(False, "--all", help="包含已停用的素材。"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
) -> None:
    """列出账号的素材库条目。"""

    async def _run() -> None:
        materials = await ItemMaterialService().list(
            account_id, enabled_only=not include_disabled, limit=limit
        )
        if not materials:
            console.print(f"[dim]账号 {account_id} 暂无素材。[/dim]")
            return
        table = Table(title=f"素材库 ({account_id}, {len(materials)})")
        for column in ("id", "名称", "类型", "大小", "标签", "启用"):
            table.add_column(column)
        for material in materials:
            table.add_row(
                str(material.local_id),
                material.name,
                material.kind,
                str(material.size_bytes),
                ",".join(material.tags) or "-",
                "Y" if material.enabled else "N",
            )
        console.print(table)

    asyncio.run(_run())


@material_app.command("enable")
def enable_material(material_id: int = typer.Option(..., "--id")) -> None:
    """启用一个素材,使其可以被新的发布引用。"""
    _set_material(material_id, True)


@material_app.command("disable")
def disable_material(material_id: int = typer.Option(..., "--id")) -> None:
    """停用一个素材;已存在的草稿引用不受影响。"""
    _set_material(material_id, False)


def _set_material(material_id: int, enabled: bool) -> None:
    async def _run() -> None:
        material = await ItemMaterialService().set_enabled(material_id, enabled)
        if material is None:
            console.print(f"[red]素材 #{material_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        state = "已启用" if material.enabled else "已停用"
        console.print(f"[green]OK[/green] 素材 #{material_id} {state}。")

    asyncio.run(_run())


def _detail_rows(item: Item) -> list[tuple[str, str]]:
    """Mirrored detail fields, rendered for the terminal."""
    values: list[tuple[str, str | None]] = [
        ("item_id", item.external_item_id),
        ("title", item.title),
        ("price", None if item.price is None else str(item.price)),
        ("description", item.description),
        ("images", str(len(item.images_or_main))),
        ("stock", None if item.stock is None else str(item.stock)),
        ("sold_count", str(item.sold_count)),
        ("status", item.status),
        ("is_on_sale", "Y" if item.is_on_sale else "N"),
        ("detail_url", item.detail_url),
        ("main_image_url", item.main_image_url),
        ("category_id", item.category_id),
        ("synced_at", format_local(item.synced_at) or None),
    ]
    return [(name, "-" if value is None else value) for name, value in values]


def _variant_table(item: Item) -> Table:
    table = Table(title=f"多规格 ({len(item.variants)})")
    for column in ("sku_id", "规格", "价格", "库存", "启用"):
        table.add_column(column)
    for variant in item.variants:
        table.add_row(
            variant.external_sku_id or "本地声明",
            variant.describe(),
            "-" if variant.price is None else str(variant.price),
            str(variant.stock),
            "Y" if variant.enabled else "N",
        )
    return table


@batch_app.command("import")
def import_batch(
    account_id: str = typer.Option(..., "--account", "-a", help="导入草稿归属的账号标识。"),
    file: Path = typer.Option(  # noqa: B008 - Typer 选项必须在默认值处构造
        ..., "--file", "-f", exists=True, dir_okay=False, readable=True
    ),
) -> None:
    """把 CSV/TSV/XLSX/ZIP 表格导入成批次草稿,并输出预览。"""

    async def _run() -> None:
        try:
            imported = _import_table(file, account_id)
        except (ValueError, OSError) as exc:
            console.print(f"[red]导入失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if not imported.drafts:
            console.print("[red]没有可导入的草稿行。[/red]")
            raise typer.Exit(code=1)
        service = ItemBatchService()
        job = await service.create_job(account_id, list(imported.drafts))
        preview = await service.preview(job.id)
        console.print(
            f"[green]OK[/green] 批次 {job.id} 已创建:草稿 {len(imported.drafts)} 行,"
            f"可发布 {preview.ready} 行,阻塞 {preview.blocked} 行。"
        )
        if imported.rejected:
            console.print(f"[yellow]跳过 {len(imported.rejected)} 行:[/yellow]")
            for row in imported.rejected[:10]:
                console.print(f"  第 {row.row_number} 行: {row.reason}")
        if imported.ignored_headers:
            console.print(f"[dim]未识别的列(忽略): {', '.join(imported.ignored_headers)}[/dim]")
        if imported.bundled_images:
            console.print(f"[dim]ZIP 内图片 {len(imported.bundled_images)} 张(未上传)。[/dim]")
        _print_preview(preview)

    asyncio.run(_run())


@batch_app.command("show")
def show_batch(job_id: str = typer.Option(..., "--job-id")) -> None:
    """查看批次进度与逐行结果。"""

    async def _run() -> None:
        job = await ItemBatchService().load_job(job_id)
        if job is None:
            console.print(f"[red]批次 {job_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        _print_job(job)

    asyncio.run(_run())


@batch_app.command("recover")
def recover_batch(job_id: str = typer.Option(..., "--job-id")) -> None:
    """重启续跑:中断的行标为需人工核对,待办行重新排队。"""

    async def _run() -> None:
        try:
            job = await ItemBatchService().recover(job_id)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print("[green]OK[/green] 批次已恢复。")
        _print_job(job)

    asyncio.run(_run())


@batch_app.command("cancel")
def cancel_batch(job_id: str = typer.Option(..., "--job-id")) -> None:
    """停止批次继续领取待办行,已记录的结果保留。"""

    async def _run() -> None:
        try:
            job = await ItemBatchService().cancel(job_id)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        _print_job(job)

    asyncio.run(_run())


@batch_app.command("retry")
def retry_batch_row(
    job_id: str = typer.Option(..., "--job-id"),
    row: int = typer.Option(..., "--row", min=1, help="要重试的行号(与 show 显示一致)"),
) -> None:
    """重试某一行;只允许失败或跳过的行,中断的行需人工核对后再重试。"""

    async def _run() -> None:
        try:
            job = await ItemBatchService().retry_entry(job_id, row - 1)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 第 {row} 行已重新排队。")
        _print_job(job)

    asyncio.run(_run())


@batch_app.command("export")
def export_batch(
    job_id: str = typer.Option(..., "--job-id"),
    out: Path | None = typer.Option(  # noqa: B008 - Typer 选项必须在默认值处构造
        None, "--out", help="写出 CSV 的路径;留空直接打到标准输出。"
    ),
) -> None:
    """把批次的逐行结果导出成 CSV(对照 Helper 的 result.csv)。"""

    async def _run() -> None:
        job = await ItemBatchService().load_job(job_id)
        if job is None:
            console.print(f"[red]批次 {job_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        rows = [
            [
                str(entry.index + 1),
                entry.state,
                entry.draft.title,
                entry.external_item_id or "",
                entry.error or "",
            ]
            for entry in job.entries
        ]
        header = ["行", "状态", "标题", "平台商品", "错误"]
        if out is None:
            console.print(",".join(header), markup=False, highlight=False, soft_wrap=True)
            for row in rows:
                console.print(
                    ",".join(_csv_cell(value) for value in row),
                    markup=False,
                    highlight=False,
                    soft_wrap=True,
                )
            return
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        writer.writerows(rows)
        # 写文件是一次本地落盘,不涉及 IO 并发;用阻塞写并把 await 语义留给调用方之外的路径。
        out.write_text(buffer.getvalue(), encoding="utf-8")  # noqa: ASYNC240
        console.print(f"[green]OK[/green] 已导出 {len(rows)} 行到 {out}。")

    asyncio.run(_run())


def _csv_cell(value: str) -> str:
    """标准输出里的极简 CSV:该引号时引号,把引号翻倍。"""
    if any(character in value for character in (",", '"', "\n", "\r")):
        return '"' + value.replace('"', '""') + '"'
    return value


@batch_app.command("retry-failed")
def retry_failed_rows(
    job_id: str = typer.Option(..., "--job-id"),
    include_interrupted: bool = typer.Option(
        False,
        "--include-interrupted",
        help="连「执行中断、未确认平台是否已创建」的行一起重排(必须先人工核对)。",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认重排被中断的行。"),
) -> None:
    """把所有失败/跳过的行重新排队(对照 Helper 的 retry-failed)。

    与参考实现的差别只有一处,但很关键:**执行中断的行默认不动**。那些行在平台侧可能已经创建了
    商品,自动重排会变成重复铺货;要带上 `--include-interrupted --yes` 明确表示已经人工核对过。
    """
    if include_interrupted and not yes:
        console.print(
            "[red]被中断的行可能在平台侧已经创建了商品;确认人工核对过再加 --yes。[/red]"
        )
        raise typer.Exit(code=1)

    async def _run() -> None:
        service = ItemBatchService()
        job = await service.load_job(job_id)
        if job is None:
            console.print(f"[red]批次 {job_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        if job.state == "cancelled":
            console.print(f"[red]批次 {job_id} 已取消,不能重排。[/red]")
            raise typer.Exit(code=1)
        retried = 0
        held: list[int] = []
        for entry in job.entries:
            if entry.state not in {"failed", "skipped"}:
                continue
            if entry.error == INTERRUPTED_ERROR and not include_interrupted:
                held.append(entry.index + 1)
                continue
            job = await service.retry_entry(job_id, entry.index)
            retried += 1
        console.print(f"[green]OK[/green] 已重排 {retried} 行。")
        if held:
            console.print(
                f"[yellow]{len(held)} 行执行中断、未确认平台侧结果,已保留待人工核对:"
                f"{held}。核对后加 --include-interrupted --yes 重排。[/yellow]"
            )
        _print_job(job)

    asyncio.run(_run())


@batch_app.command("run")
def run_batch(
    job_id: str = typer.Option(..., "--job-id"),
    max_rows: int = typer.Option(0, "--max-rows", min=0, help="0=按批次剩余行执行"),
    yes: bool = typer.Option(False, "--yes", help="确认真实发布(会调用闲鱼,尚未真实校准)"),
) -> None:
    """执行批次:先跳过预览阻塞的行,再逐行上传图片并发布。"""

    async def _run() -> None:
        service = ItemBatchService()
        job = await service.load_job(job_id)
        if job is None:
            console.print(f"[red]批次 {job_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        if not yes:
            console.print("[yellow]未加 --yes,只显示当前批次与预览,未执行任何发布。[/yellow]")
            _print_job(job)
            _print_preview(await service.preview(job_id))
            return
        publisher = ItemPublishService(XianyuItemWriter(), materials=ItemMaterialService())
        runner = ItemBatchRunner(publisher.as_publisher(), service=service)
        try:
            summary = await runner.run(job_id, max_rows=max_rows or None)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]OK[/green] 批次 {job_id}:尝试 {summary.attempted},"
            f"成功 {summary.succeeded},失败 {summary.failed},跳过 {summary.skipped}。"
        )
        if summary.stopped_reason:
            console.print(f"[yellow]{summary.stopped_reason}[/yellow]")
        updated = await service.load_job(job_id)
        if updated is not None:
            _print_job(updated)

    asyncio.run(_run())


@automation_app.command("set")
def set_item_automation(
    account_id: str = typer.Option(..., "--account", "-a"),
    item_id: str = typer.Option(..., "--item-id"),
    mode: str = typer.Option(
        ..., "--mode", help="exclude 完全排除 / keep 强制保留 / clear 恢复由策略判定"
    ),
    note: str = typer.Option("", "--note", help="原因;keep 必填。"),
    actor: str = typer.Option("operator", "--by", help="操作人标识。"),
) -> None:
    """设置单品级人工覆盖(exclude / keep / clear)。

    人工决定先于自动策略:`exclude` 让商品完全不出现在生命周期计划里,`keep` 让它在计划里永远是
    KEEP。`clear` 恢复由策略判定。
    """

    async def _run() -> None:
        normalized = mode.strip().lower()
        if normalized not in {"exclude", "keep", "clear"}:
            console.print("[red]--mode 只能是 exclude / keep / clear[/red]")
            raise typer.Exit(code=1)
        resolved = None if normalized == "clear" else normalized
        try:
            written = await domain_items.set_automation_override(
                account_id, item_id, mode=resolved, note=note or None, actor=actor
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if not written:
            console.print(f"[red]商品 {item_id} 不在账号 {account_id} 的本地镜像里[/red]")
            raise typer.Exit(code=1)
        label = {"exclude": "已排除在自动化之外", "keep": "已锁定为保留", "clear": "已恢复策略判定"}
        console.print(f"[green]OK[/green] {item_id} {label[normalized]}")

    asyncio.run(_run())


@automation_app.command("list")
def list_item_automation(
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """列出该账号所有带人工覆盖的商品。"""

    async def _run() -> None:
        rows = await domain_items.list_automation_overrides(account_id)
        if not rows:
            console.print("[dim]该账号没有人工覆盖的商品。[/dim]")
            return
        table = Table(title=f"商品级人工覆盖 ({account_id}, {len(rows)})")
        for column in ("item_id", "title", "mode", "note", "by", "at"):
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(
                row.external_item_id,
                row.title,
                row.automation_override or "-",
                row.automation_note or "-",
                row.automation_overridden_by or "-",
                format_local(row.automation_overridden_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@lifecycle_app.command("plan")
def plan_lifecycle(  # noqa: PLR0917 - Typer 命令参数本身就是关键字参数
    account_id: str = typer.Option(..., "--account", "-a"),
    delist_after_days: int = typer.Option(30, "--delist-after-days", min=1),
    delete_after_days: int = typer.Option(0, "--delete-after-days", min=0, help="0=不自动删除"),
    daily_limit: int = typer.Option(0, "--daily-limit", min=0, help="0=不限"),
    include_keyword: list[str] | None = typer.Option(  # noqa: B008 - Typer 选项
        None, "--include-keyword"
    ),
    exclude_keyword: list[str] | None = typer.Option(  # noqa: B008 - Typer 选项
        None, "--exclude-keyword"
    ),
    min_price: float = typer.Option(0, "--min-price", min=0),
    max_price: float = typer.Option(0, "--max-price", min=0),
) -> None:
    """Dry Run:按策略列出每个商品的判定,不改动任何商品。"""

    async def _run() -> None:
        plan = await ItemLifecycleService().plan(
            account_id,
            policy=lifecycle_policy(
                delist_after_days=delist_after_days,
                delete_after_days=delete_after_days,
                daily_limit=daily_limit,
            ),
            item_filter=lifecycle_item_filter(
                include_keywords=include_keyword or (),
                exclude_keywords=exclude_keyword or (),
                min_price=min_price,
                max_price=max_price,
            ),
        )
        _print_plan(plan)

    asyncio.run(_run())


@lifecycle_app.command("apply")
def apply_lifecycle(  # noqa: PLR0917 - Typer 命令参数本身就是关键字参数
    account_id: str = typer.Option(..., "--account", "-a"),
    delist_after_days: int = typer.Option(30, "--delist-after-days", min=1),
    delete_after_days: int = typer.Option(0, "--delete-after-days", min=0),
    daily_limit: int = typer.Option(0, "--daily-limit", min=0),
    include_keyword: list[str] | None = typer.Option(  # noqa: B008 - Typer 选项
        None, "--include-keyword"
    ),
    exclude_keyword: list[str] | None = typer.Option(  # noqa: B008 - Typer 选项
        None, "--exclude-keyword"
    ),
    min_price: float = typer.Option(0, "--min-price", min=0),
    max_price: float = typer.Option(0, "--max-price", min=0),
    yes: bool = typer.Option(False, "--yes", help="确认执行 Dry Run 结果。"),
    allow_delete: bool = typer.Option(False, "--allow-delete", help="允许执行删除动作。"),
) -> None:
    """执行 Dry Run 的结果;未加 --yes 只输出判定,未配置执行器时拒绝执行。"""

    async def _run() -> None:
        plan = await ItemLifecycleService().plan(
            account_id,
            policy=lifecycle_policy(
                delist_after_days=delist_after_days,
                delete_after_days=delete_after_days,
                daily_limit=daily_limit,
            ),
            item_filter=lifecycle_item_filter(
                include_keywords=include_keyword or (),
                exclude_keywords=exclude_keyword or (),
                min_price=min_price,
                max_price=max_price,
            ),
        )
        _print_plan(plan)
        if not yes:
            console.print("[yellow]未加 --yes,只输出 Dry Run 结果,未执行任何动作。[/yellow]")
            return
        try:
            result = await ItemLifecycleService().apply(plan, allow_delete=allow_delete)
        except RuntimeError as exc:
            console.print(f"[red]未执行:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"执行 {result.executed} 项,未执行 {result.failed} 项。")

    asyncio.run(_run())


def _import_table(path: Path, account_id: str) -> ImportResult:
    """Dispatch a table file to the importer that matches its extension."""
    suffix = path.suffix.lower()
    payload = path.read_bytes()
    if suffix == ".xlsx":
        return parse_xlsx(payload, account_id=account_id)
    if suffix == ".zip":
        return parse_zip_bundle(payload, account_id=account_id)
    text = payload.decode("utf-8-sig", errors="replace")
    if suffix == ".tsv":
        return parse_tsv(text, account_id=account_id)
    if suffix in {".csv", ".txt"}:
        return parse_csv(text, account_id=account_id)
    msg = f"不支持的表格格式: {suffix or path.name}"
    raise ValueError(msg)


def _print_preview(preview: BatchPreview) -> None:
    table = Table(title=f"批量铺货预览 (可发布 {preview.ready} / {preview.total})")
    for column in ("行", "标题", "价格", "问题"):
        table.add_column(column)
    for row in preview.rows:
        problems = "; ".join(problem.detail for problem in row.problems)
        table.add_row(
            str(row.index + 1),
            row.draft.title,
            "-" if row.draft.price is None else str(row.draft.price),
            problems or "OK",
        )
    console.print(table)


def _print_job(job: BatchPublishJob) -> None:
    progress = job.progress
    console.print(
        f"批次 {job.id}:{job.state} "
        f"(待办 {progress.pending} / 运行中 {progress.running} / "
        f"成功 {progress.succeeded} / 失败 {progress.failed} / 跳过 {progress.skipped})"
    )
    table = Table(title="逐行状态")
    for column in ("行", "状态", "标题", "平台商品", "错误"):
        table.add_column(column)
    for entry in job.entries:
        table.add_row(
            str(entry.index + 1),
            entry.state,
            entry.draft.title,
            entry.external_item_id or "-",
            entry.error or "-",
        )
    console.print(table)


def _print_plan(plan: object) -> None:
    rows = getattr(plan, "rows", ())
    counts = getattr(plan, "counts", {})
    console.print(f"Dry Run:{counts}")
    if getattr(plan, "truncated", False):
        console.print(
            f"[yellow]注意[/yellow] 本次只扫描了前 {getattr(plan, 'scanned', 0)} 件在售商品,"
            "镜像里还有更多未纳入判定;需要完整覆盖时请缩小筛选范围或分批执行。"
        )
    table = Table(title="生命周期判定")
    for column in ("item_id", "动作", "原因"):
        table.add_column(column)
    for row in rows:
        table.add_row(row.item.external_item_id, row.decision.action, row.decision.reason)
    console.print(table)
