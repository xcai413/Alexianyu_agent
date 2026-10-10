"""FastAPI app exposing domain operations; fastapi-mcp converts these into MCP tools.

Conventions:
  - Every endpoint returns {"success": bool, "data": ... | "error": str}
  - Endpoints are thin wrappers over the domain layer (the single source of truth).
"""

from __future__ import annotations

import uuid
from base64 import b64decode
from binascii import Error as Base64Error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from xianyu_agent.application.item import (
    ItemBatchService,
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
from xianyu_agent.config import get_settings
from xianyu_agent.domain import (
    accounts as domain_accounts,
    cards as domain_cards,
    messages as domain_messages,
    orders as domain_orders,
    rules as domain_rules,
)
from xianyu_agent.domain.item.entity import Item
from xianyu_agent.domain.item.sku import normalize_specs
from xianyu_agent.domain.item.sku_binding import (
    SpecBinding,
    list_item_bindings,
    replace_item_bindings,
)
from xianyu_agent.interfaces.web.auth.router import router as web_auth_router
from xianyu_agent.interfaces.web.dashboard.router import router as web_dashboard_router
from xianyu_agent.interfaces.web.operations.router import router as web_operations_router
from xianyu_agent.protocol.items_client import ItemSyncError
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailError
from xianyu_agent.protocol.mtop.endpoints.item_publish import XianyuItemWriter
from xianyu_agent.protocol.orders_client import OrderSyncError, XianyuOrdersClient
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.services.account_lock import (
    AccountConnectionAlreadyRunningError,
    AccountConnectionLock,
)
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.services.heartbeat import purge_old_messages
from xianyu_agent.utils.time_utils import format_local

app = FastAPI(title="xianyu-agent", description="闲鱼运营 Agent API", version="0.1.0")
app.include_router(web_auth_router)
app.include_router(web_dashboard_router)
app.include_router(web_operations_router)


class SpaStaticFiles(StaticFiles):
    """Static files with an index fallback for browser-side Web routes."""

    async def get_response(self, path: str, scope: Any) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


class AccountCreate(BaseModel):
    account_id: str = Field(..., description="账号标识")
    nickname: str | None = None
    remark: str | None = None
    enabled: bool = True


class AccountEnabled(BaseModel):
    enabled: bool


class CookieLogin(BaseModel):
    account_id: str
    cookie: str
    remark: str | None = None


class MessageListQuery(BaseModel):
    account_id: str
    since_hours: float = 1.0
    limit: int = 20


class RuleCreate(BaseModel):
    name: str
    type: str = "keyword"
    pattern: str = ""
    reply_text: str
    account_id: str | None = None
    priority: int = 100


class RuleEnabled(BaseModel):
    enabled: bool


class RuleTest(BaseModel):
    content: str
    account_id: str | None = None


class CardCreate(BaseModel):
    account_id: str
    name: str
    content: str | None = None
    type: str = "text"
    unit_price: float = 0.0
    description: str | None = None


class CardRestock(BaseModel):
    content: str


class CardEnabled(BaseModel):
    enabled: bool


class ItemSyncRequest(BaseModel):
    account_id: str
    page_size: int = Field(default=20, ge=1, le=50)
    max_pages: int = Field(default=100, ge=1, le=100)


class OrderSyncRequest(BaseModel):
    account_id: str
    page_size: int = Field(default=30, ge=1, le=50)
    max_pages: int = Field(default=100, ge=1, le=100)


class PurgeRequest(BaseModel):
    older_than_hours: float = 24.0


class ItemBatchImportRequest(BaseModel):
    """One table handed to a batch import.

    Small tables go in as text; XLSX/ZIP are binary, so they arrive base64
    encoded. The filename decides which importer runs, never a content guess.
    """

    account_id: str
    filename: str = Field(..., description="表格文件名,用于按扩展名选择导入器")
    content: str | None = Field(default=None, description="CSV/TSV 文本内容")
    content_base64: str | None = Field(default=None, description="XLSX/ZIP 的 base64 内容")


class ItemLifecycleApplyRequest(BaseModel):
    account_id: str
    delist_after_days: int = Field(default=30, ge=1)
    delete_after_days: int = Field(default=0, ge=0, description="0=不自动删除")
    daily_limit: int = Field(default=0, ge=0, description="0=不限")
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    confirm: bool = Field(default=False, description="必须显式确认才会执行")
    allow_delete: bool = Field(default=False, description="允许执行删除动作")


class ItemBatchRunRequest(BaseModel):
    """Execute a stored batch; publishing is a real external action."""

    confirm: bool = Field(default=False, description="必须显式确认才会真实发布")
    max_rows: int = Field(default=0, ge=0, description="0=按批次剩余行执行")


class ItemDetailRequest(BaseModel):
    account_id: str
    item_id: str = Field(..., description="平台商品 id")


class SpecBindingInput(BaseModel):
    specs: dict[str, str] = Field(..., description="规格名 -> 规格值")
    target_kind: str = Field(default="card", description="库存来源类型,当前只有 card")
    target_id: int = Field(..., gt=0)
    quantity: int = Field(default=1, ge=1, description="每次发货取几份")
    enabled: bool = True


class SpecBindingsRequest(BaseModel):
    account_id: str
    item_id: str
    bindings: list[SpecBindingInput] = Field(
        default_factory=list, description="整组替换;传空列表表示清空"
    )


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _err(exc: Exception) -> NoReturn:
    raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/v1/accounts", operation_id="account_create")
async def account_create(body: AccountCreate) -> dict[str, Any]:
    try:
        row = await domain_accounts.create_account(
            body.account_id, nickname=body.nickname, remark=body.remark, enabled=body.enabled
        )
    except Exception as exc:
        _err(exc)
    return _ok({"id": row.id, "account_id": row.account_id})


@app.get("/api/v1/accounts", operation_id="account_list")
async def account_list() -> dict[str, Any]:
    rows = await domain_accounts.list_accounts()
    return _ok(
        [
            {
                "account_id": r.account_id,
                "enabled": r.enabled,
                "status": r.status,
                "last_heartbeat_at": format_local(r.last_heartbeat_at) or None,
            }
            for r in rows
        ]
    )


@app.get("/api/v1/accounts/{account_id}", operation_id="account_get")
async def account_get(account_id: str) -> dict[str, Any]:
    row = await domain_accounts.get_account(account_id)
    if row is None:
        _err(ValueError(f"账号 {account_id} 不存在"))
    return _ok({"account_id": row.account_id, "enabled": row.enabled, "status": row.status})


@app.put("/api/v1/accounts/{account_id}/enabled", operation_id="account_set_enabled")
async def account_set_enabled(account_id: str, body: AccountEnabled) -> dict[str, Any]:
    ok = await domain_accounts.set_enabled(account_id, body.enabled)
    if not ok:
        _err(ValueError(f"账号 {account_id} 不存在"))
    return _ok({"account_id": account_id, "enabled": body.enabled})


@app.delete("/api/v1/accounts/{account_id}", operation_id="account_delete")
async def account_delete(account_id: str) -> dict[str, Any]:
    ok = await domain_accounts.delete_account(account_id)
    if not ok:
        _err(ValueError(f"账号 {account_id} 不存在"))
    return _ok({"deleted": account_id})


@app.post("/api/v1/auth/login", operation_id="auth_login")
async def auth_login(body: CookieLogin) -> dict[str, Any]:
    lock = AccountConnectionLock(get_settings().account_lock_path(body.account_id))
    try:
        lock.acquire(owner_id=f"api:auth-login:{uuid.uuid4().hex}")
    except AccountConnectionAlreadyRunningError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"账号 {body.account_id} 正在运行,请先停止 Worker",
        ) from exc
    signer = CookieSigner()
    try:
        saved = await signer.save_cookie(body.account_id, body.cookie)
        if not saved:
            _err(ValueError(f"账号 {body.account_id} 不存在或 Fernet 未配置"))
    finally:
        lock.release()
    return _ok({"account_id": body.account_id, "cookie_saved": True})


@app.get("/api/v1/auth/status/{account_id}", operation_id="auth_status")
async def auth_status(account_id: str) -> dict[str, Any]:
    fp = await CookieSigner().fingerprint(account_id)
    if fp is None:
        _err(ValueError(f"账号 {account_id} 无 Cookie"))
    return _ok(fp)


@app.get("/api/v1/messages", operation_id="message_list")
async def message_list(
    account_id: str, since_hours: float = 1.0, limit: int = 20
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    rows = await domain_messages.list_recent(account_id=account_id, since=since, limit=limit)
    return _ok(
        [
            {
                "id": m.id,
                "chat_id": m.chat_id,
                "direction": m.direction,
                "sender": m.sender_name or m.sender_id,
                "content": m.content,
                "received_at": format_local(m.received_at) or None,
            }
            for m in rows
        ]
    )


@app.get("/api/v1/orders", operation_id="order_list")
async def order_list(account_id: str, status: str = "", limit: int = 20) -> dict[str, Any]:
    rows = await domain_orders.list_for_account(account_id, status=status or None, limit=limit)
    return _ok(
        [
            {
                "id": o.id,
                "order_id": o.order_id,
                "item_title": o.item_title,
                "amount": o.amount,
                "quantity": o.quantity,
                "status": o.status,
                "placed_at": format_local(o.placed_at) or None,
                "delivery_content": o.delivery_content,
                "delivery_fail_reason": o.delivery_fail_reason,
            }
            for o in rows
        ]
    )


@app.get("/api/v1/orders/{order_id}", operation_id="order_get")
async def order_get(order_id: int) -> dict[str, Any]:
    row = await domain_orders.get_by_id(order_id)
    if row is None:
        _err(ValueError(f"订单 #{order_id} 不存在"))
    return _ok(
        {
            "id": row.id,
            "order_id": row.order_id,
            "item_title": row.item_title,
            "amount": row.amount,
            "quantity": row.quantity,
            "status": row.status,
            "placed_at": format_local(row.placed_at) or None,
            "delivery_content": row.delivery_content,
            "delivery_fail_reason": row.delivery_fail_reason,
        }
    )


@app.post("/api/v1/orders/sync", operation_id="order_sync")
async def order_sync(body: OrderSyncRequest) -> dict[str, Any]:
    """只读同步卖家已售订单到本地镜像, 不触发发货。"""
    try:
        snapshot = await XianyuOrdersClient().fetch_all_sold(
            body.account_id,
            page_size=body.page_size,
            max_pages=body.max_pages,
        )
        result = await domain_orders.apply_sold_orders_snapshot(body.account_id, snapshot.orders)
    except (OrderSyncError, ValueError) as exc:
        _err(exc)
    return _ok(
        {
            "account_id": result.account_id,
            "reported_total": snapshot.reported_total,
            "pages": snapshot.pages,
            "total": result.total,
            "created": result.created,
            "updated": result.updated,
        }
    )


@app.post("/api/v1/items/sync", operation_id="item_sync")
async def item_sync(body: ItemSyncRequest) -> dict[str, Any]:
    """只读同步在售商品到本地镜像,并刷新已售件数;不修改闲鱼商品。"""
    try:
        outcome = await ItemSyncService().sync_on_sale(
            body.account_id, page_size=body.page_size, max_pages=body.max_pages
        )
    except (ItemSyncError, ValueError) as exc:
        _err(exc)
    result = outcome.result
    return _ok(
        {
            "account_id": result.account_id,
            "total": result.total,
            "pages": outcome.pages,
            "created": result.created,
            "updated": result.updated,
            "marked_off_sale": result.marked_off_sale,
            "sold_counts_updated": outcome.sold_counts_updated,
        }
    )


@app.get("/api/v1/items", operation_id="item_list")
async def item_list(
    account_id: str, include_off_sale: bool = False, limit: int = 100
) -> dict[str, Any]:
    """列出本地商品镜像;已售件数取镜像值,由同步负责刷新。"""
    items = await ItemQueryService().list_items(
        account_id, on_sale_only=not include_off_sale, limit=limit
    )
    return _ok([_item_summary_payload(item) for item in items])


@app.get("/api/v1/items/{item_pk}", operation_id="item_get")
async def item_get(item_pk: int) -> dict[str, Any]:
    """读取一个商品镜像,含规格(多规格单元)。"""
    item = await ItemQueryService().get_item_by_local_id(item_pk)
    if item is None:
        _err(ValueError(f"商品镜像 #{item_pk} 不存在"))
    return _ok(_item_detail_payload(item))


@app.post("/api/v1/items/detail-sync", operation_id="item_detail_sync")
async def item_detail_sync(body: ItemDetailRequest) -> dict[str, Any]:
    """读取单个商品的平台详情,记录当前可命名的字段(多规格标记)。"""
    try:
        facts = await ItemDetailService().sync_facts(body.account_id, body.item_id)
    except (ItemDetailError, ValueError) as exc:
        _err(exc)
    if facts is None:
        _err(ValueError(f"商品 {body.item_id} 不在本地镜像里,请先同步商品列表"))
    return _ok(
        {
            "item_id": facts.item_id,
            "multi_spec": facts.multi_spec,
            "publisher_identified": facts.publisher_id is not None,
            "note": "详情里的标题、描述与图片字段路径尚未校准,本次只记录多规格标记",
        }
    )


@app.post("/api/v1/rules", operation_id="rule_create")
async def rule_create(body: RuleCreate) -> dict[str, Any]:
    try:
        row = await domain_rules.create_rule(
            body.name,
            body.type,
            body.pattern,
            body.reply_text,
            account_id=body.account_id,
            priority=body.priority,
        )
    except Exception as exc:
        _err(exc)
    return _ok({"id": row.id, "name": row.name, "type": row.type})


@app.get("/api/v1/rules", operation_id="rule_list")
async def rule_list(account_id: str = "") -> dict[str, Any]:
    rows = await domain_rules.list_rules(account_id=account_id or None)
    return _ok(
        [
            {
                "id": r.id,
                "name": r.name,
                "type": r.type,
                "pattern": r.pattern,
                "reply_text": r.reply_text,
                "priority": r.priority,
                "enabled": r.enabled,
                "hit_count": r.hit_count,
            }
            for r in rows
        ]
    )


@app.put("/api/v1/rules/{rule_id}/enabled", operation_id="rule_set_enabled")
async def rule_set_enabled(rule_id: int, body: RuleEnabled) -> dict[str, Any]:
    ok = await domain_rules.set_rule_enabled(rule_id, body.enabled)
    if not ok:
        _err(ValueError(f"规则 #{rule_id} 不存在"))
    return _ok({"id": rule_id, "enabled": body.enabled})


@app.delete("/api/v1/rules/{rule_id}", operation_id="rule_delete")
async def rule_delete(rule_id: int) -> dict[str, Any]:
    ok = await domain_rules.delete_rule(rule_id)
    if not ok:
        _err(ValueError(f"规则 #{rule_id} 不存在"))
    return _ok({"deleted": rule_id})


@app.post("/api/v1/rules/test", operation_id="rule_test")
async def rule_test(body: RuleTest) -> dict[str, Any]:
    target = body.account_id or "__global__"
    hits = await domain_rules.match_for_account(target, body.content)
    return _ok(
        [{"id": r.id, "name": r.name, "type": r.type, "reply_text": r.reply_text} for r in hits]
    )


@app.post("/api/v1/cards", operation_id="card_create")
async def card_create(body: CardCreate) -> dict[str, Any]:
    try:
        row = await domain_cards.create_card(
            body.account_id,
            body.name,
            body.content,
            type_=body.type,
            unit_price=body.unit_price,
            description=body.description,
        )
    except Exception as exc:
        _err(exc)
    return _ok({"id": row.id, "name": row.name, "total": row.total, "remaining": row.remaining})


@app.get("/api/v1/cards", operation_id="card_list")
async def card_list(account_id: str = "") -> dict[str, Any]:
    rows = await domain_cards.list_cards(account_id=account_id or None)
    return _ok(
        [
            {
                "id": c.id,
                "name": c.name,
                "account_id": c.account_id,
                "type": c.type,
                "total": c.total,
                "remaining": c.remaining,
                "enabled": c.enabled,
            }
            for c in rows
        ]
    )


@app.post("/api/v1/cards/{card_id}/restock", operation_id="card_restock")
async def card_restock(card_id: int, body: CardRestock) -> dict[str, Any]:
    row = await domain_cards.restock(card_id, body.content)
    if row is None:
        _err(ValueError(f"卡 #{card_id} 不存在"))
    return _ok({"id": row.id, "total": row.total, "remaining": row.remaining})


@app.post("/api/v1/cards/{card_id}/consume", operation_id="card_consume")
async def card_consume(card_id: int) -> dict[str, Any]:
    code = await domain_cards.consume_card(card_id, None)
    if code is None:
        _err(ValueError(f"卡 #{card_id} 无可用卡密"))
    return _ok({"card_id": card_id, "code": code})


@app.put("/api/v1/cards/{card_id}/enabled", operation_id="card_set_enabled")
async def card_set_enabled(card_id: int, body: CardEnabled) -> dict[str, Any]:
    ok = await domain_cards.set_card_enabled(card_id, body.enabled)
    if not ok:
        _err(ValueError(f"卡 #{card_id} 不存在"))
    return _ok({"id": card_id, "enabled": body.enabled})


@app.delete("/api/v1/cards/{card_id}", operation_id="card_delete")
async def card_delete(card_id: int) -> dict[str, Any]:
    ok = await domain_cards.delete_card(card_id)
    if not ok:
        _err(ValueError(f"卡 #{card_id} 不存在"))
    return _ok({"deleted": card_id})


@app.post("/api/v1/maintenance/purge-messages", operation_id="maintenance_purge_messages")
async def maintenance_purge_messages(body: PurgeRequest) -> dict[str, Any]:
    deleted = await purge_old_messages(older_than_hours=body.older_than_hours)
    return _ok({"deleted": deleted})


@app.get("/api/v1/pool/status", operation_id="pool_status")
async def pool_status() -> dict[str, Any]:
    rows = await AccountPool().status()
    return _ok(rows)


# 批次的路径刻意与 /api/v1/items/{item_pk} 分开:后者按主键解析路径参数,
# 共用前缀会让 batches 被当成主键,出现 422 而不是应有的处理。


@app.post("/api/v1/item-batches", operation_id="item_batch_create")
async def item_batch_create(body: ItemBatchImportRequest) -> dict[str, Any]:
    """导入表格并建立批次(只建草稿与预览,不发布)。"""
    try:
        imported = _import_batch_table(body)
    except ValueError as exc:
        _err(exc)
    if not imported.drafts:
        _err(ValueError("没有可导入的草稿行"))
    service = ItemBatchService()
    job = await service.create_job(body.account_id, list(imported.drafts))
    preview = await service.preview(job.id)
    return _ok(
        {
            "job": _job_payload(job),
            "preview": _preview_payload(preview),
            "rejected": [
                {"row_number": row.row_number, "reason": row.reason} for row in imported.rejected
            ],
            "ignored_headers": list(imported.ignored_headers),
            "bundled_images": list(imported.bundled_images),
        }
    )


@app.get("/api/v1/item-batches", operation_id="item_batch_list")
async def item_batch_list(account_id: str, limit: int = 20) -> dict[str, Any]:
    jobs = await ItemBatchService().list_jobs(account_id, limit=limit)
    return _ok([_job_payload(job, include_entries=False) for job in jobs])


@app.get("/api/v1/item-batches/{job_id}", operation_id="item_batch_get")
async def item_batch_get(job_id: str) -> dict[str, Any]:
    job = await ItemBatchService().load_job(job_id)
    if job is None:
        _err(ValueError(f"批次 {job_id} 不存在"))
    return _ok(_job_payload(job))


@app.post("/api/v1/item-batches/{job_id}/recover", operation_id="item_batch_recover")
async def item_batch_recover(job_id: str) -> dict[str, Any]:
    """重启续跑:中断的行标为需人工核对,待办行重新排队。"""
    try:
        job = await ItemBatchService().recover(job_id)
    except ValueError as exc:
        _err(exc)
    return _ok(_job_payload(job))


@app.post("/api/v1/item-batches/{job_id}/cancel", operation_id="item_batch_cancel")
async def item_batch_cancel(job_id: str) -> dict[str, Any]:
    try:
        job = await ItemBatchService().cancel(job_id)
    except ValueError as exc:
        _err(exc)
    return _ok(_job_payload(job))


@app.post(
    "/api/v1/item-batches/{job_id}/entries/{index}/retry",
    operation_id="item_batch_retry_entry",
)
async def item_batch_retry_entry(job_id: str, index: int) -> dict[str, Any]:
    """重试某一行(只允许失败或跳过的行);中断的行要人工核对后再重试。"""
    try:
        job = await ItemBatchService().retry_entry(job_id, index)
    except ValueError as exc:
        _err(exc)
    return _ok(_job_payload(job))


@app.post("/api/v1/item-batches/{job_id}/run", operation_id="item_batch_run")
async def item_batch_run(job_id: str, body: ItemBatchRunRequest) -> dict[str, Any]:
    """执行批次:先跳过预览阻塞的行,再逐行上传并发布。

    写入类协议尚未真实校准:没有可用的签名凭证时每一行都会以失败回执,批次不会
    假装成功;显式 confirm 是唯一的开启方式。
    """
    if not body.confirm:
        _err(ValueError("未确认执行:请显式传 confirm=true"))
    service = ItemBatchService()
    if await service.load_job(job_id) is None:
        _err(ValueError(f"批次 {job_id} 不存在"))
    publisher = ItemPublishService(XianyuItemWriter(), materials=ItemMaterialService())
    runner = ItemBatchRunner(publisher.as_publisher(), service=service)
    summary = await runner.run(job_id, max_rows=body.max_rows or None)
    job = await service.load_job(job_id)
    return _ok(
        {
            "job": _job_payload(job) if job is not None else None,
            "attempted": summary.attempted,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "skipped": summary.skipped,
            "stopped_reason": summary.stopped_reason,
        }
    )


@app.get("/api/v1/item-lifecycle/plan", operation_id="item_lifecycle_plan")
async def item_lifecycle_plan(  # noqa: PLR0917 - 查询参数本身就是关键字参数
    account_id: str,
    delist_after_days: int = 30,
    delete_after_days: int = 0,
    daily_limit: int = 0,
    include_keyword: Annotated[list[str] | None, Query()] = None,
    exclude_keyword: Annotated[list[str] | None, Query()] = None,
    min_price: float | None = None,
    max_price: float | None = None,
) -> dict[str, Any]:
    """Dry Run:列出每个商品的判定,不改动任何商品。"""
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
    return _ok(_plan_payload(plan))


@app.post("/api/v1/item-lifecycle/apply", operation_id="item_lifecycle_apply")
async def item_lifecycle_apply(body: ItemLifecycleApplyRequest) -> dict[str, Any]:
    """执行 Dry Run 结果;未确认或没有执行器时拒绝执行。"""
    if not body.confirm:
        _err(ValueError("未确认执行:请显式传 confirm=true"))
    service = ItemLifecycleService()
    policy = lifecycle_policy(
        delist_after_days=body.delist_after_days,
        delete_after_days=body.delete_after_days,
        daily_limit=body.daily_limit,
    )
    plan = await service.plan(
        body.account_id,
        policy=policy,
        item_filter=lifecycle_item_filter(
            include_keywords=body.include_keywords,
            exclude_keywords=body.exclude_keywords,
            min_price=body.min_price,
            max_price=body.max_price,
        ),
    )
    try:
        result = await service.apply(plan, allow_delete=body.allow_delete)
    except RuntimeError as exc:
        _err(exc)
    return _ok(
        {
            "plan": _plan_payload(plan),
            "executed": result.executed,
            "failed": result.failed,
            "results": [
                {
                    "item_id": row.item_id,
                    "action": row.action,
                    "executed": row.executed,
                    "error": row.error,
                }
                for row in result.results
            ],
        }
    )


def _import_batch_table(body: ItemBatchImportRequest) -> ImportResult:
    """Pick an importer by file extension; never guess from the content."""
    suffix = Path(body.filename).suffix.lower()
    if body.content_base64 is not None:
        try:
            raw = b64decode(body.content_base64, validate=True)
        except (Base64Error, ValueError) as exc:
            _err(ValueError(f"content_base64 不是合法 base64: {exc}"))
        if suffix == ".xlsx":
            return parse_xlsx(raw, account_id=body.account_id)
        if suffix == ".zip":
            return parse_zip_bundle(raw, account_id=body.account_id)
        _err(ValueError(f"不支持 base64 传参的表格格式: {suffix or body.filename}"))
    if body.content is None:
        _err(ValueError("必须提供 content 或 content_base64"))
    if suffix == ".tsv":
        return parse_tsv(body.content, account_id=body.account_id)
    if suffix in {".csv", ".txt"}:
        return parse_csv(body.content, account_id=body.account_id)
    _err(ValueError(f"不支持的表格格式: {suffix or body.filename}"))


def _job_payload(job: Any, *, include_entries: bool = True) -> dict[str, Any]:
    """Serialize one batch job; entries are omitted for list responses."""
    progress = job.progress
    payload: dict[str, Any] = {
        "id": job.id,
        "account_id": job.account_id,
        "state": job.state,
        "total": progress.total,
        "pending": progress.pending,
        "running": progress.running,
        "succeeded": progress.succeeded,
        "failed": progress.failed,
        "skipped": progress.skipped,
        "created_at": format_local(job.created_at) or None,
        "started_at": format_local(job.started_at) or None,
        "finished_at": format_local(job.finished_at) or None,
    }
    if include_entries:
        payload["entries"] = [
            {
                "index": entry.index,
                "state": entry.state,
                "title": entry.draft.title,
                "source_row": entry.draft.source_row,
                "external_item_id": entry.external_item_id,
                "error": entry.error,
                "attempts": entry.attempts,
            }
            for entry in job.entries
        ]
    return payload


def _preview_payload(preview: Any) -> dict[str, Any]:
    return {
        "total": preview.total,
        "ready": preview.ready,
        "blocked": preview.blocked,
        "rows": [
            {
                "index": row.index,
                "title": row.draft.title,
                "source_row": row.draft.source_row,
                "problems": [problem.detail for problem in row.problems],
            }
            for row in preview.rows
        ],
    }


def _plan_payload(plan: Any) -> dict[str, Any]:
    return {
        "account_id": plan.account_id,
        "generated_at": format_local(plan.generated_at) or None,
        "counts": plan.counts,
        "rows": [
            {
                "item_id": row.item.external_item_id,
                "title": row.item.title,
                "action": row.decision.action,
                "reason": row.decision.reason,
            }
            for row in plan.rows
        ],
    }


def _item_summary_payload(item: Item) -> dict[str, Any]:
    """Item fields for list responses; sold_count is the mirror value from sync."""
    return {
        "id": item.local_id,
        "item_id": item.external_item_id,
        "title": item.title,
        "price": None if item.price is None else str(item.price),
        "sold_count": item.sold_count,
        "has_multi_spec": item.has_multi_spec,
        "variant_count": len(item.variants),
        "is_on_sale": item.is_on_sale,
        "detail_url": item.detail_url,
        "main_image_url": item.main_image_url,
        "last_synced_at": format_local(item.synced_at) or None,
    }


def _item_detail_payload(item: Item) -> dict[str, Any]:
    """Item fields plus its specs for one-item responses."""
    return {
        **_item_summary_payload(item),
        "description": item.description,
        "images": list(item.images_or_main),
        "stock": item.stock,
        "status": item.status,
        "category_id": item.category_id,
        "first_seen_at": format_local(item.listed_at) or None,
        "variants": [
            {
                "sku_id": variant.external_sku_id,
                "specs": [{"name": spec.name, "value": spec.value} for spec in variant.specs],
                "price": None if variant.price is None else str(variant.price),
                "stock": variant.stock,
                "enabled": variant.enabled,
            }
            for variant in item.variants
        ],
    }


@app.get("/api/v1/item-spec-bindings", operation_id="item_spec_binding_list")
async def item_spec_binding_list(account_id: str, item_id: str) -> dict[str, Any]:
    """列出某个商品的"规格组合 -> 库存来源"映射。"""
    bindings = await list_item_bindings(account_id, item_id)
    return _ok([_binding_payload(binding) for binding in bindings])


@app.put("/api/v1/item-spec-bindings", operation_id="item_spec_binding_replace")
async def item_spec_binding_replace(body: SpecBindingsRequest) -> dict[str, Any]:
    """整组替换某个商品的规格库存映射;传空列表表示清空。"""
    try:
        written = await replace_item_bindings(
            body.account_id,
            body.item_id,
            tuple(_binding_from_input(entry) for entry in body.bindings),
        )
        bindings = await list_item_bindings(body.account_id, body.item_id)
    except ValueError as exc:
        _err(exc)
    return _ok(
        {"written": written, "bindings": [_binding_payload(binding) for binding in bindings]}
    )


def _binding_payload(binding: SpecBinding) -> dict[str, Any]:
    return {
        "key": binding.key,
        "specs": {spec.name: spec.value for spec in binding.specs},
        "target_kind": binding.target_kind,
        "target_id": binding.target_id,
        "quantity": binding.quantity,
        "enabled": binding.enabled,
    }


def _binding_from_input(entry: SpecBindingInput) -> SpecBinding:
    return SpecBinding(
        specs=normalize_specs(entry.specs),
        target_kind=entry.target_kind,
        target_id=entry.target_id,
        quantity=entry.quantity,
        enabled=entry.enabled,
    )


def mount_web_console(application: FastAPI, *, static_dir: Path | None = None) -> None:
    """Serve the built Web console when its static bundle is present.

    API routes are registered before this mount. The MCP CLI moves this mount
    behind the dynamically registered transport routes. Development uses Vite because
    ``frontend/dist`` is intentionally absent until a production build exists.
    """
    bundle_dir = static_dir or Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if (bundle_dir / "index.html").is_file():
        application.mount("/", SpaStaticFiles(directory=bundle_dir, html=True), name="web-console")


mount_web_console(app)
