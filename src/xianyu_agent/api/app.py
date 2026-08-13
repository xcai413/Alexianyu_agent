"""FastAPI app exposing domain operations; fastapi-mcp converts these into MCP tools.

Conventions:
  - Every endpoint returns {"success": bool, "data": ... | "error": str}
  - Endpoints are thin wrappers over the domain layer (the single source of truth).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from xianyu_agent.config import get_settings
from xianyu_agent.domain import (
    accounts as domain_accounts,
    cards as domain_cards,
    items as domain_items,
    messages as domain_messages,
    orders as domain_orders,
    rules as domain_rules,
)
from xianyu_agent.protocol.items_client import ItemSyncError, XianyuItemsClient
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.services.account_lock import (
    AccountConnectionAlreadyRunningError,
    AccountConnectionLock,
)
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.services.heartbeat import purge_old_messages
from xianyu_agent.utils.time_utils import format_local

app = FastAPI(title="xianyu-agent", description="闲鱼运营 Agent API", version="0.1.0")


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


class PurgeRequest(BaseModel):
    older_than_hours: float = 24.0


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _err(exc: Exception) -> None:
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
                "status": o.status,
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
            "status": row.status,
            "delivery_content": row.delivery_content,
            "delivery_fail_reason": row.delivery_fail_reason,
        }
    )


@app.post("/api/v1/items/sync", operation_id="item_sync")
async def item_sync(body: ItemSyncRequest) -> dict[str, Any]:
    """只读同步账号在售商品到本地镜像。"""
    try:
        snapshot = await XianyuItemsClient().fetch_all_on_sale(
            body.account_id, page_size=body.page_size, max_pages=body.max_pages
        )
        result = await domain_items.apply_on_sale_snapshot(
            body.account_id, snapshot.items, synced_at=snapshot.fetched_at
        )
    except (ItemSyncError, ValueError) as exc:
        _err(exc)
    return _ok(
        {
            "account_id": result.account_id,
            "total": result.total,
            "pages": snapshot.pages,
            "created": result.created,
            "updated": result.updated,
            "marked_off_sale": result.marked_off_sale,
        }
    )


@app.get("/api/v1/items", operation_id="item_list")
async def item_list(
    account_id: str, include_off_sale: bool = False, limit: int = 100
) -> dict[str, Any]:
    rows = await domain_items.list_items(
        account_id, on_sale_only=not include_off_sale, limit=limit
    )
    return _ok(
        [
            {
                "id": item.id,
                "item_id": item.item_id,
                "title": item.title,
                "price": item.price,
                "status": item.status,
                "is_on_sale": item.is_on_sale,
                "detail_url": item.detail_url,
                "main_image_url": item.main_image_url,
                "last_synced_at": format_local(item.last_synced_at) or None,
            }
            for item in rows
        ]
    )


@app.get("/api/v1/items/{item_pk}", operation_id="item_get")
async def item_get(item_pk: int) -> dict[str, Any]:
    item = await domain_items.get_item(item_pk)
    if item is None:
        _err(ValueError(f"商品镜像 #{item_pk} 不存在"))
    return _ok(
        {
            "id": item.id,
            "item_id": item.item_id,
            "title": item.title,
            "price": item.price,
            "status": item.status,
            "is_on_sale": item.is_on_sale,
            "detail_url": item.detail_url,
            "main_image_url": item.main_image_url,
            "category_id": item.category_id,
            "auction_type": item.auction_type,
            "first_seen_at": format_local(item.first_seen_at) or None,
            "last_synced_at": format_local(item.last_synced_at) or None,
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
