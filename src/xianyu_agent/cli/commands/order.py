"""`xianyu-agent order ...` subcommands."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from xianyu_agent.application.session import (
    CredentialResult,
    CredentialResultState,
    CredentialSupervisor,
)
from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, get_async_session
from xianyu_agent.domain.order import orders as domain_orders
from xianyu_agent.infrastructure.session import LegacyValidationGate, LegacyWsCredentialBackend
from xianyu_agent.protocol.client import WsClient
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.protocol.mtop.endpoints.adjust_price import (
    AdjustPriceError,
    XianyuAdjustPriceClient,
    parse_price_to_cents,
)
from xianyu_agent.protocol.mtop.endpoints.buyer_flower import (
    BuyerFlowerError,
    XianyuBuyerFlowerClient,
)
from xianyu_agent.protocol.mtop.endpoints.consign import (
    ConsignError,
    XianyuConsignClient,
)
from xianyu_agent.protocol.mtop.endpoints.order_read import (
    OrderReadError,
    XianyuOrderReadClient,
)
from xianyu_agent.protocol.orders_client import OrderSyncError, XianyuOrdersClient
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.services.account_lock import (
    AccountConnectionAlreadyRunningError,
    AccountConnectionLock,
)
from xianyu_agent.services.delivery_service import DeliveryService
from xianyu_agent.services.guardrails import Guardrails
from xianyu_agent.utils.time_utils import format_local

app = typer.Typer(help="订单查询。")
console = Console()


def _print_order_read_error(exc: Exception) -> None:
    console.print(f"[red]订单附加信息读取失败:[/red] {exc}")
    raise typer.Exit(code=1) from exc


def _print_order_read(data: dict[str, object]) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False))


def _build_credential_supervisor() -> CredentialSupervisor:
    """Build the canonical credential application boundary for standalone delivery."""
    signer = CookieSigner()
    return CredentialSupervisor(
        LegacyWsCredentialBackend(signer=signer),
        LegacyValidationGate(),
    )


def _raise_credential_preparation_failure(result: CredentialResult[object]) -> None:
    """Render only policy-classified, secret-free standalone preparation failures."""
    if result.state is CredentialResultState.NEEDS_VALIDATION:
        console.print(
            "[red]账号凭据需要 App 人工验证,未启动 WS。[/red] 完成验证后先执行 auth refresh。"
        )
    elif result.state is CredentialResultState.RETRYABLE_FAILURE:
        console.print("[red]凭据准备暂时失败,未启动 WS;请稍后重试。[/red]")
    else:
        console.print(
            "[red]账号凭据不可用,未启动 WS。[/red] "
            "请先检查 auth status,并按需执行 auth login / auth refresh。"
        )
    raise typer.Exit(code=2)


async def _start_with_prepared_credentials(
    account_id: str,
    client: WsClient,
    account_lock: AccountConnectionLock,
) -> None:
    """Atomically prepare canonical credentials and hand the same lock to transport."""
    try:
        account_lock.acquire_for_transport_handoff(
            owner_id=f"cli:order-redeliver:{uuid.uuid4().hex}"
        )
    except AccountConnectionAlreadyRunningError as exc:
        console.print(f"[red]账号连接已被占用:{exc}[/red]")
        raise typer.Exit(code=2) from exc

    try:
        result = await _build_credential_supervisor().ensure(account_id)
    except Exception as exc:
        account_lock.release()
        console.print("[red]凭据准备失败,未启动 WS。[/red]")
        raise typer.Exit(code=2) from exc

    if result.state is not CredentialResultState.SUCCESS:
        account_lock.release()
        _raise_credential_preparation_failure(result)

    # WsClient.start consumes the one-shot handoff on this exact lock instance;
    # the underlying cross-process lock is never released between prepare/start.
    client.start()


@app.command("sync")
def sync_orders(
    account_id: str = typer.Option(..., "--account", "-a", help="已扫码登录的账号标识。"),
    page_size: int = typer.Option(30, "--page-size", min=1, max=50),
    max_pages: int = typer.Option(100, "--max-pages", min=1, max=100),
) -> None:
    """只读同步卖家已售订单到本地镜像, 不触发发货或修改闲鱼订单。"""

    async def _run() -> None:
        try:
            snapshot = await XianyuOrdersClient().fetch_all_sold(
                account_id,
                page_size=page_size,
                max_pages=max_pages,
            )
            result = await domain_orders.apply_sold_orders_snapshot(account_id, snapshot.orders)
        except (OrderSyncError, ValueError) as exc:
            console.print(f"[red]同步失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]OK[/green] {account_id} 卖家订单镜像已同步: "
            f"远端报告 {snapshot.reported_total} 条 / {snapshot.pages} 页, "
            f"本次解析 {result.total} 条, 新增 {result.created}, 更新 {result.updated}。"
        )

    asyncio.run(_run())


@app.command("details")
def order_details(
    order_id: str = typer.Argument(..., help="订单号。"),
    account_id: str = typer.Option(..., "--account", "-a"),
) -> None:
    """只读展示平台可获取的订单、收货、规格、物流和退款详情。"""
    _print_order_read(asyncio.run(XianyuOrderReadClient().fetch_details(account_id, order_id)))


@app.command("consign-info")
def consign_info(
    account_id: str = typer.Option(..., "--account", "-a"),
    order_id: str = typer.Option(..., "--order-id"),
) -> None:
    """只读读取订单规格、收货摘要和支持的发货方式。"""

    async def _run() -> None:
        try:
            result = await XianyuOrderReadClient().fetch_consign_info(account_id, order_id)
        except (OrderReadError, ValueError) as exc:
            _print_order_read_error(exc)
            return
        _print_order_read(result)

    asyncio.run(_run())


@app.command("logistics")
def logistics(
    account_id: str = typer.Option(..., "--account", "-a"),
    order_id: str = typer.Option(..., "--order-id"),
) -> None:
    """只读读取订单物流状态与轨迹。"""

    async def _run() -> None:
        try:
            result = await XianyuOrderReadClient().fetch_logistics(account_id, order_id)
        except (OrderReadError, ValueError) as exc:
            _print_order_read_error(exc)
            return
        _print_order_read(result)

    asyncio.run(_run())


@app.command("refund-record")
def refund_record(
    account_id: str = typer.Option(..., "--account", "-a"),
    order_id: str = typer.Option(..., "--order-id"),
) -> None:
    """只读读取订单退款记录摘要。"""

    async def _run() -> None:
        try:
            result = await XianyuOrderReadClient().fetch_refund_record(account_id, order_id)
        except (OrderReadError, ValueError) as exc:
            _print_order_read_error(exc)
            return
        _print_order_read(result)

    asyncio.run(_run())


@app.command("request-flower")
def request_flower(
    account_id: str = typer.Option(..., "--account", "-a", help="订单所属的账号标识。"),
    order_id: str = typer.Option(..., "--order-id", help="要索要小红花的订单 id。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认向买家真实发送求花消息。"),
) -> None:
    """向买家索要小红花(真实平台动作,会给买家发一条消息)。

    该接口只有单一参考来源且未在真实账号校准,因此只提供单订单显式触发,
    不接入定时或批量自动化;同一订单重复调用会重复打扰买家,调用方必须自行幂等。
    """
    if not yes:
        console.print("[red]求花会向买家真实发送消息,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            result = await XianyuBuyerFlowerClient().request_flower(account_id, order_id)
        except (BuyerFlowerError, ValueError) as exc:
            console.print(f"[red]求花失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 已向订单 {result.order_id} 的买家发送求花请求。")

    asyncio.run(_run())


@app.command("confirm-shipment")
def confirm_shipment(
    account_id: str = typer.Option(..., "--account", "-a", help="订单所属的账号标识。"),
    order_id: str = typer.Option(..., "--order-id", help="要确认发货的订单 id。"),
    trade_text: str = typer.Option(
        "", "--trade-text", help="随确认一起提交的发货凭证文本,留空表示不带凭证。"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认在平台上真实确认发货。"),
) -> None:
    """在订单上确认发货(真实平台动作,未真实校准)。

    自动发货链路由发货策略的 platform_confirm 触发;这个命令只用于单订单显式校准,
    不接入定时或批量自动化。同一订单重复调用会由平台按业务错误返回。
    """
    if not yes:
        console.print("[red]确认发货会真实改变平台订单状态,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        try:
            result = await XianyuConsignClient().confirm_delivery(
                account_id, order_id, trade_text=trade_text
            )
        except (ConsignError, ValueError) as exc:
            console.print(f"[red]确认发货失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]OK[/green] 订单 {result.order_id} 已在平台确认发货。")

    asyncio.run(_run())


@app.command("adjust-price")
def adjust_price(
    account_id: str = typer.Option(..., "--account", "-a", help="订单所属的账号标识。"),
    order_id: str = typer.Option(..., "--order-id", help="要改价的待付款订单 id。"),
    price: str = typer.Option(..., "--price", help="改价后的订单总价(元),最多两位小数。"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认在平台上真实修改订单价格。"),
) -> None:
    """修改待付款订单价格(真实平台动作,未真实校准)。

    平台只接受买家尚未付款的订单;改价后买家按新价格付款。该命令只做单订单显式触发,
    不接入自动流程:批量改价需要先有真实校准结果和运营规则。
    """
    if not yes:
        console.print("[red]改价会真实改变买家待付金额,请加 --yes 明确确认。[/red]")
        raise typer.Exit(code=1)
    try:
        price_cents = parse_price_to_cents(price)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    async def _run() -> None:
        try:
            result = await XianyuAdjustPriceClient().adjust_order_price(
                account_id, order_id, price_cents
            )
        except (AdjustPriceError, ValueError) as exc:
            console.print(f"[red]改价失败:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]OK[/green] 订单 {result.order_id} 已改价为 "
            f"{result.price_cents / 100:.2f} 元,等买家付款。"
        )

    asyncio.run(_run())


@app.command("list")
def list_orders(
    account_id: str = typer.Option(..., "--account", "-a"),
    status: str = typer.Option("", "--status", "-s", help="过滤状态,如 paid / delivered。"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=500),
) -> None:
    """列出某账号订单。"""

    async def _run() -> None:
        rows = await domain_orders.list_for_account(account_id, status=status or None, limit=limit)
        if not rows:
            console.print(f"[dim]账号 {account_id} 无订单。[/dim]")
            return
        table = Table(title=f"订单 ({account_id})")
        table.add_column("id")
        table.add_column("order_id")
        table.add_column("item", overflow="fold")
        table.add_column("amount")
        table.add_column("quantity")
        table.add_column("status", style="magenta")
        table.add_column("placed_at")
        table.add_column("paid_at")
        table.add_column("delivered_at")
        for r in rows:
            table.add_row(
                str(r.id),
                r.order_id,
                (r.item_title or "-")[:30],
                str(r.amount),
                str(r.quantity),
                r.status,
                format_local(r.placed_at) or "-",
                format_local(r.paid_at) or "-",
                format_local(r.delivered_at) or "-",
            )
        console.print(table)

    asyncio.run(_run())


@app.command("show")
def show_order(order_id: int = typer.Argument(...)) -> None:
    """显示订单详情(含发货内容 / 失败原因)。"""

    async def _run() -> None:
        r = await domain_orders.get_by_id(order_id)
        if r is None:
            console.print(f"[red]订单 #{order_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        async with get_async_session() as session:
            account_row = (
                await session.execute(select(Account).where(Account.id == r.account_id).limit(1))
            ).scalar_one_or_none()
        account_key = account_row.account_id if account_row is not None else str(r.account_id)
        console.rule(f"订单 #{r.id}")
        console.print(f"order_id:   {r.order_id}")
        console.print(f"account:    {account_key}")
        console.print(f"item:       {r.item_title or '-'} ({r.item_id or '-'})")
        console.print(f"buyer:      {r.buyer_name or '-'} ({r.buyer_id or '-'})")
        console.print(f"amount:     {r.amount}")
        console.print(f"quantity:   {r.quantity}")
        console.print(f"status:     {r.status}")
        console.print(f"placed_at:  {format_local(r.placed_at) or '-'}")
        console.print(f"paid_at:    {format_local(r.paid_at) or '-'}")
        console.print(f"delivered:  {format_local(r.delivered_at) or '-'}")
        # delivery_content 只有旧卡密链路会写;Phase 6 发货走任务表,内容不落在订单行上。
        # 所以这里不能把"没有内容"说成"未发货",否则已发货的订单也会显示未发货。
        delivered = r.delivered_at is not None or r.status in {"delivered", "completed"}
        console.print(f"发货内容:   [cyan]{r.delivery_content or '—'}[/cyan]")
        console.print(
            "发货状态:   "
            + ("已发货" if delivered else "尚未发货")
            + "[dim](Phase 6 发货不把卡密写进订单行,内容见发货任务)[/dim]"
            if delivered
            else "发货状态:   尚未发货"
        )
        console.print(f"失败原因:   {r.delivery_fail_reason or '-'}")

    asyncio.run(_run())


@app.command("redeliver")
def redeliver_order(
    order_id: int = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认向买家真实补发卡密。"),
) -> None:
    """重试发货(发送失败过的订单,复用已预留的卡密)。

    这是旧卡密链路的单次补发入口:它会自己开一条 WebSocket 把卡密发给买家,所以要求 `--yes`。
    走策略发货的订单请用 `delivery job run`(默认 Dry Run),两边不要混用。
    """
    if not yes:
        console.print(
            "[red]这会向买家真实补发卡密,请加 --yes 明确确认。[/red]"
            "\n[dim]只是想看看会发什么?先用 `delivery job plan` 做 Dry Run。[/dim]"
        )
        raise typer.Exit(code=1)

    async def _run() -> None:
        r = await domain_orders.get_by_id(order_id)
        if r is None:
            console.print(f"[red]订单 #{order_id} 不存在。[/red]")
            raise typer.Exit(code=1)
        if r.status != "paid":
            console.print(f"[yellow]订单状态 {r.status} 不是 paid,无需重试发货。[/yellow]")
            raise typer.Exit(code=1)
        async with get_async_session() as session:
            account_row = (
                await session.execute(select(Account).where(Account.id == r.account_id).limit(1))
            ).scalar_one_or_none()
        account_key = account_row.account_id if account_row is not None else str(r.account_id)
        settings = get_settings()
        if not settings.ws_url:
            console.print(
                "[red]未配置 XIANYU_WS_URL,无法真实发送。[/red]"
                "\n请先配置 WS URL 后重试,或在线运行 worker(pool start)。"
            )
            raise typer.Exit(code=1)

        connected = asyncio.Event()

        async def _on_state(state) -> None:
            if state.state == ConnectionState.CONNECTED:
                connected.set()

        account_lock = AccountConnectionLock(settings.account_lock_path(account_key))
        client = WsClient(account_key, on_state=_on_state, account_lock=account_lock)
        await _start_with_prepared_credentials(account_key, client, account_lock)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(connected.wait(), timeout=10.0)
            if client.state != ConnectionState.CONNECTED:
                console.print(f"[red]WS 连接未就绪(state={client.state.value}),重试取消。[/red]")
                raise typer.Exit(code=1)

            async def sender(_a: str, _o: str, content: str) -> bool:
                return await client.send_text(content)

            svc = DeliveryService(sender=sender, guardrails=Guardrails())
            result = await svc.retry(order_id)
        finally:
            await client.stop()
        if result.delivered:
            console.print(
                f"[green]重试成功:[/green] 订单 {result.order_id} 已发货 "
                f"(code={result.code or '-'})。"
            )
        else:
            console.print(f"[red]重试失败:[/red] {result.order_id} -> {result.reason}")
            raise typer.Exit(code=1)

    asyncio.run(_run())
