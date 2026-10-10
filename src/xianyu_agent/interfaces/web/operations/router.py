"""Read-only operational projections for the administrator Web console."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from xianyu_agent.application.attention import AttentionService
from xianyu_agent.application.automation import (
    PlatformAutomationService,
    enqueue_immediate_run,
)
from xianyu_agent.application.backup import BackupService
from xianyu_agent.application.chat import ChatAssistService
from xianyu_agent.application.delivery import (
    ApiConfigService,
    DeliveryJobService,
    DeliveryPolicyService,
    DeliveryTemplateService,
)
from xianyu_agent.application.delivery.executor import DeliveryExecutor, DeliveryRequest
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryQueryService,
    InventoryService,
)
from xianyu_agent.application.item import ItemDetailService, ItemQueryService
from xianyu_agent.application.item.lifecycle_service import (
    ItemLifecycleService,
    lifecycle_item_filter,
    lifecycle_policy,
)
from xianyu_agent.application.item.platform_guard import AccountCoolingError
from xianyu_agent.application.notify import NotificationChannelService
from xianyu_agent.application.ports.secret_vault import SecretMetadata, SecretRef, SecretValue
from xianyu_agent.config import get_settings
from xianyu_agent.db import (
    Account,
    AlertDispatch,
    AuditLog,
    Conversation,
    MessageFilterRule,
    get_async_session,
)
from xianyu_agent.db.models import DeliveryJob
from xianyu_agent.domain import (
    accounts as domain_accounts,
    cards as domain_cards,
    items as domain_items,
    messages as domain_messages,
    orders as domain_orders,
    rules as domain_rules,
)
from xianyu_agent.domain.inventory.unit import masked_preview
from xianyu_agent.domain.runtime import command as worker_commands, daemon as runtime_daemon
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault
from xianyu_agent.interfaces.web.auth.router import require_admin, require_admin_csrf
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailError
from xianyu_agent.protocol.mtop.endpoints.order_read import (
    OrderReadError,
    XianyuOrderReadClient,
)
from xianyu_agent.protocol.mtop.endpoints.user_profile import (
    UserProfileError,
    XianyuUserProfileClient,
)
from xianyu_agent.runtime.account_pool import AccountPool
from xianyu_agent.services.logging_setup import redact_text, summarize_log_levels
from xianyu_agent.utils.time_utils import format_local

router = APIRouter(prefix="/api/v1/web/operations", tags=["web-operations"])
AdminSession = Annotated[dict[str, object], Depends(require_admin)]
AdminCsrfSession = Annotated[dict[str, object], Depends(require_admin_csrf)]


class WorkerCommandRequest(BaseModel):
    action: str


class LocalItemUpdateRequest(BaseModel):
    """修改本地商品目录,不代表修改闲鱼平台商品。"""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    price: str | None = Field(default=None, max_length=64)
    category_id: str | None = Field(default=None, max_length=128)
    detail_url: str | None = Field(default=None, max_length=1024)
    stock: int | None = Field(default=None, ge=0)


class ConversationTakeoverRequest(BaseModel):
    state: str


class ConversationReplyRequest(BaseModel):
    text: str
    confirm: bool = False


class DeliveryPlanRequest(BaseModel):
    account_id: str
    order_id: str
    item_id: str | None = None
    buyer_id: str | None = None
    quantity: int | None = Field(default=None, ge=0)


class DeliveryRunRequest(BaseModel):
    account_id: str
    order_id: str
    item_id: str | None = None
    quantity: int | None = Field(default=None, ge=0)
    confirm: bool = False


class ApiDeliveryConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=512)
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = "{}"
    response_path: str = "card"
    timeout_seconds: float = Field(default=10.0, ge=0.5, le=60.0)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=255)


class ApiDeliveryConfigUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=1, max_length=512)
    method: str | None = Field(default=None, min_length=1, max_length=16)
    headers: dict[str, str] | None = None
    body: str | None = None
    response_path: str | None = Field(default=None, min_length=1, max_length=255)
    timeout_seconds: float | None = Field(default=None, ge=0.5, le=60.0)
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=255)


class ApiSecretRequest(BaseModel):
    value: SecretStr = Field(min_length=1)
    label: str = Field(default="delivery_api", max_length=80)


class ApiConfigEnabledRequest(BaseModel):
    enabled: bool


class CardUnitImportRequest(BaseModel):
    """卡券(卡密)逐行导入;单次上限 1 MiB,避免把管理接口当批量传输通道。"""

    contents: str = Field(min_length=1, max_length=1024 * 1024)


class ItemAutomationRequest(BaseModel):
    """单品级人工覆盖:exclude 完全排除,keep 强制保留,clear 恢复策略判定。"""

    account_id: str = Field(min_length=1, max_length=64)
    item_id: str = Field(min_length=1, max_length=128)
    mode: str = Field(pattern="^(exclude|keep|clear)$")
    note: str | None = Field(default=None, max_length=255)


class QuickReplyRequest(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=2000)
    account_id: str | None = Field(default=None, max_length=64)
    sort_order: int = Field(default=0, ge=0)
    category: str = Field(default="默认", min_length=1, max_length=64)


class QuickReplyUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=64)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


class BuyerNoteRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    buyer_id: str = Field(min_length=1, max_length=64)
    note: str = Field(min_length=1, max_length=2000)


class MessageFilterRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    keyword: str = Field(min_length=1, max_length=255)
    filter_type: str = Field(default="skip_reply", pattern="^(skip_reply|skip_notify)$")
    enabled: bool = True


class MessageFilterUpdateRequest(BaseModel):
    keyword: str | None = Field(default=None, min_length=1, max_length=255)
    filter_type: str | None = Field(default=None, pattern="^(skip_reply|skip_notify)$")
    enabled: bool | None = None


class MessageFilterBatchRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    keywords: list[str] = Field(min_length=1, max_length=200)
    filter_type: str = Field(default="skip_reply", pattern="^(skip_reply|skip_notify)$")
    enabled: bool = True


class MessageFilterBatchDeleteRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)


class ReplyPauseRequest(BaseModel):
    """账号级自动回复暂停:minutes 为空表示立即恢复。"""

    minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)
    reason: str | None = Field(default=None, max_length=255)


class RunNowRequest(BaseModel):
    """让 Agent/运营立刻排一次平台动作;门禁与定时执行完全一致。"""

    account_id: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=32)


class DeliveryTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=2000)
    description: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class DeliveryTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    description: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class NotificationChannelRequest(BaseModel):
    """新建/覆盖通知渠道;`secret_ref` 是 SecretVault 的不透明引用,不是密钥本身。"""

    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=32)
    target: str = Field(min_length=1, max_length=2048)
    secret_ref: str | None = Field(default=None, max_length=64)
    events: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=255)


class NotificationChannelUpdateRequest(BaseModel):
    """更新通知渠道;渠道类型保持不变,避免把已有订阅悄悄改成另一种协议。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    target: str | None = Field(default=None, min_length=1, max_length=2048)
    secret_ref: str | None = Field(default=None, max_length=64)
    events: list[str] | None = None
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=255)


class NotificationBindingRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)


class DeliveryJobResolveRequest(BaseModel):
    """人工收尾一条结果不明的发货任务;`note` 是核对依据,必填。"""

    delivered: bool
    note: str = Field(min_length=1, max_length=255)
    resolved_by: str | None = Field(default=None, max_length=64)


class ReplyRecordsResetRequest(BaseModel):
    """清空某账号的回复投递记录(纯本地);`rule_id` 为空表示清全部。"""

    account_id: str = Field(min_length=1, max_length=64)
    rule_id: int | None = Field(default=None, ge=1)


class BackupImportRequest(BaseModel):
    """导入备份文档;默认只预演,不接收服务器文件路径。"""

    document: dict[str, object]
    dry_run: bool = True


@router.get("/chat/reply-records")
async def reply_records(
    _: AdminSession,
    account_id: str,
    rule_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """回复投递记录:只回长度与前 8 位预览,不回完整正文。"""
    try:
        rows = await domain_rules.list_reply_logs(account_id, rule_id=rule_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}) from exc
    return {
        "data": [
            {
                "id": row.id,
                "rule_id": row.rule_id,
                "rule_name": row.rule_name,
                "conversation_id": row.conversation_id,
                "success": row.success,
                "source": row.source,
                "sent_at": format_local(row.sent_at) or None,
                "text_length": row.text_length,
                "text_preview": row.text_preview,
                "has_image": row.has_image,
                "error": row.error,
            }
            for row in rows
        ]
    }


@router.post("/chat/reply-records/reset")
async def reset_reply_records(
    body: ReplyRecordsResetRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """清空回复投递记录,让 `--reply-once` 规则可以重新触发;只删去重记录,不删消息与审计。"""
    try:
        removed = await domain_rules.clear_reply_logs(
            body.account_id, rule_id=body.rule_id, actor="web"
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}) from exc
    return {"data": {"account_id": body.account_id, "removed": removed}}


class InventoryPoolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: str = "api"
    account_scope: str | None = Field(default=None, max_length=64)
    item_scope: str | None = Field(default=None, max_length=128)
    sku_scope: str | None = Field(default=None, max_length=255)
    api_config_ref: str | None = Field(default=None, max_length=128)
    content: str | None = None
    image_url: str | None = Field(default=None, max_length=1024)
    delay_seconds: int = Field(default=0, ge=0, le=3600)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=255)


class InventoryPoolUpdateRequest(BaseModel):
    """修改库存池定义;不允许通过 Web 修改池类型或 API 引用。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=255)
    content: str | None = None
    image_url: str | None = Field(default=None, max_length=1024)
    account_scope: str | None = Field(default=None, max_length=64)
    item_scope: str | None = Field(default=None, max_length=128)
    sku_scope: str | None = Field(default=None, max_length=255)
    delay_seconds: int | None = Field(default=None, ge=0, le=3600)
    enabled: bool | None = None
    clear_scope: bool = False


class DeliveryPolicyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    inventory_pool_id: int = Field(gt=0)
    account_id: str | None = Field(default=None, max_length=64)
    item_id: str | None = Field(default=None, max_length=128)
    sku_id: str | None = Field(default=None, max_length=255)
    delay_seconds: int = Field(default=0, ge=0, le=3600)
    template: str | None = None
    template_ref: str | None = Field(default=None, max_length=128)
    platform_confirm: bool = False
    priority: int = Field(default=0, ge=0)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=255)


class DeliveryPolicyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    inventory_pool_id: int | None = Field(default=None, gt=0)
    account_id: str | None = Field(default=None, max_length=64)
    item_id: str | None = Field(default=None, max_length=128)
    sku_id: str | None = Field(default=None, max_length=255)
    delay_seconds: int | None = Field(default=None, ge=0, le=3600)
    delivery_mode: str | None = Field(default=None, max_length=16)
    template: str | None = None
    template_ref: str | None = Field(default=None, max_length=128)
    platform_confirm: bool | None = None
    priority: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None
    clear_scope: bool = False


class PlatformAutomationRequest(BaseModel):
    action: str
    enabled: bool = False
    schedule_time: str | None = None
    delay_minutes: int | None = Field(default=None, ge=0, le=43200)
    quiet_hours: str | None = None


class AutomationCalibrationRequest(BaseModel):
    action: str
    confirm: bool = False


class AutomationRunResolveRequest(BaseModel):
    """人工收尾结果不明的自动化执行;依据必填,避免无凭据改写事实。"""

    status: str
    note: str = Field(min_length=1, max_length=200)


def _api_secret_vault() -> SqlAlchemySecretVault:
    return SqlAlchemySecretVault(FernetKeyring.from_environment())


@router.get("/accounts")
async def accounts(_: AdminSession) -> dict[str, object]:
    """Expose the same persisted account-pool projection as the CLI, read-only."""
    return {"data": await AccountPool().status()}


@router.get("/backup/export")
async def backup_export(
    _: AdminSession,
    account_id: str | None = None,
    include_cards: bool = False,
) -> dict[str, object]:
    """导出运营配置;默认不含卡密正文,且永远不含凭据与真实校准记录。"""
    document = await BackupService().export_document(
        include_cards=include_cards, account_id=account_id or None
    )
    return {"data": document}


@router.post("/backup/import")
async def backup_import(
    body: BackupImportRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """导入运营配置;默认 Dry Run,写入必须由请求体显式设置 dry_run=false。"""
    try:
        report = await BackupService().import_document(body.document, dry_run=body.dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "BACKUP_REJECTED"}) from exc
    mode = "dry_run" if body.dry_run else "apply"
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="backup.imported",
                target="configuration",
                params={"mode": mode, "created": report.created, "skipped": report.skipped},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "mode": mode,
            "created": report.created,
            "skipped": report.skipped,
            "total_created": report.total_created,
            "total_skipped": report.total_skipped,
        }
    }


@router.post("/accounts/{account_id}/reply-pause")
async def set_account_reply_pause(
    account_id: str,
    body: ReplyPauseRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """暂停或恢复账号级自动回复;到点自动恢复,不需要人工清理。"""
    until = None
    if body.minutes is not None:
        until = datetime.now(UTC) + timedelta(minutes=body.minutes)
    ok = await domain_accounts.set_reply_pause(
        account_id, until=until, reason=body.reason if until is not None else None
    )
    if not ok:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="account.reply_pause_changed",
                target=account_id,
                params={
                    "paused": until is not None,
                    "minutes": body.minutes,
                    "has_reason": bool(body.reason),  # 原因正文不进审计
                },
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "account_id": account_id,
            "paused_until": format_local(until) if until is not None else None,
        }
    }


@router.get("/chat/quick-replies")
async def quick_replies(
    _: AdminSession,
    account_id: str | None = None,
    include_disabled: bool = False,
) -> dict[str, object]:
    """列出快捷回复;只服务人工客服,不参与自动回复。"""
    try:
        rows = await ChatAssistService().list_quick_replies(
            account_id=account_id or None, include_disabled=include_disabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}) from exc
    return {
        "data": [
            {
                "id": row.id,
                "account_id": row.account_id,
                "title": row.title,
                "content": row.content,
                "category": row.category,
                "sort_order": row.sort_order,
                "enabled": row.enabled,
                "use_count": row.use_count,
            }
            for row in rows
        ]
    }


@router.post("/chat/quick-replies")
async def create_quick_reply(
    body: QuickReplyRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """新增快捷回复(全局或账号专属)。"""
    try:
        row = await ChatAssistService().add_quick_reply(
            title=body.title,
            content=body.content,
            account_id=body.account_id,
            sort_order=body.sort_order,
            category=body.category,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "QUICK_REPLY_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="chat.quick_reply_created",
                target=row.title,
                params={"quick_reply_id": row.id, "scope": row.account_id or "global"},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": row.id, "title": row.title}}


@router.put("/chat/quick-replies/{reply_id}")
async def update_quick_reply(
    reply_id: int, body: QuickReplyUpdateRequest, _: AdminCsrfSession
) -> dict[str, object]:
    """更新快捷回复/短语。"""
    if not body.model_dump(exclude_unset=True):
        raise HTTPException(status_code=400, detail={"code": "QUICK_REPLY_REJECTED"})
    try:
        row = await ChatAssistService().update_quick_reply(
            reply_id,
            title=body.title,
            content=body.content,
            category=body.category,
            sort_order=body.sort_order,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "QUICK_REPLY_REJECTED"}) from exc
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "QUICK_REPLY_NOT_FOUND"})
    return {"data": {"id": row.id, "title": row.title, "enabled": row.enabled}}


@router.post("/chat/quick-replies/{reply_id}/use")
async def use_quick_reply(reply_id: int, _: AdminCsrfSession) -> dict[str, object]:
    """记录人工使用一次快捷短语,不触发平台发送。"""
    if not await ChatAssistService().use_quick_reply(reply_id):
        raise HTTPException(status_code=404, detail={"code": "QUICK_REPLY_NOT_FOUND"})
    return {"data": {"id": reply_id, "used": True}}


@router.delete("/chat/quick-replies/{reply_id}")
async def delete_quick_reply(
    reply_id: int,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """删除一条快捷回复。"""
    removed = await ChatAssistService().delete_quick_reply(reply_id)
    if not removed:
        raise HTTPException(status_code=404, detail={"code": "QUICK_REPLY_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="chat.quick_reply_deleted",
                target=str(reply_id),
                params={},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": reply_id, "deleted": True}}


@router.get("/chat/message-filters")
async def message_filters(
    _: AdminSession, account_id: str | None = None, filter_type: str | None = None
) -> dict[str, object]:
    """列出账号级消息过滤规则。"""
    try:
        rows = await ChatAssistService().list_message_filters(
            account_id=account_id, filter_type=filter_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_FILTER_REJECTED"}) from exc
    return {
        "data": [
            {
                "id": row.id,
                "account_id": row.account_id,
                "keyword": row.keyword,
                "filter_type": row.filter_type,
                "enabled": row.enabled,
            }
            for row in rows
        ]
    }


@router.post("/chat/message-filters")
async def create_message_filter(
    body: MessageFilterRequest, _: AdminCsrfSession
) -> dict[str, object]:
    """新增账号级消息过滤规则。"""
    try:
        row = await ChatAssistService().create_message_filter(
            account_id=body.account_id,
            keyword=body.keyword,
            filter_type=body.filter_type,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_FILTER_REJECTED"}) from exc
    return {
        "data": {
            "id": row.id,
            "account_id": row.account_id,
            "keyword": row.keyword,
            "filter_type": row.filter_type,
            "enabled": row.enabled,
        }
    }


@router.post("/chat/message-filters/{filter_id}/toggle")
async def toggle_message_filter(filter_id: int, _: AdminCsrfSession) -> dict[str, object]:
    """启用或停用一条消息过滤规则。"""
    async with get_async_session() as session:
        row = await session.get(MessageFilterRule, filter_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_FILTER_NOT_FOUND"})
        enabled = not row.enabled
    await ChatAssistService().set_message_filter_enabled(filter_id, enabled)
    return {"data": {"id": filter_id, "enabled": enabled}}


@router.put("/chat/message-filters/{filter_id}")
async def update_message_filter(
    filter_id: int, body: MessageFilterUpdateRequest, _: AdminCsrfSession
) -> dict[str, object]:
    """更新过滤规则内容或类型。"""
    try:
        row = await ChatAssistService().update_message_filter(
            filter_id, keyword=body.keyword, filter_type=body.filter_type, enabled=body.enabled
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_FILTER_REJECTED"}) from exc
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_FILTER_NOT_FOUND"})
    return {
        "data": {
            "id": row.id,
            "account_id": row.account_id,
            "keyword": row.keyword,
            "filter_type": row.filter_type,
            "enabled": row.enabled,
        }
    }


@router.post("/chat/message-filters/batch-create")
async def batch_create_message_filters(
    body: MessageFilterBatchRequest, _: AdminCsrfSession
) -> dict[str, object]:
    """批量新增过滤规则,重复项会跳过。"""
    try:
        created, skipped = await ChatAssistService().batch_create_message_filters(
            account_id=body.account_id,
            keywords=body.keywords,
            filter_type=body.filter_type,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_FILTER_REJECTED"}) from exc
    return {"data": {"created": created, "skipped": skipped}}


@router.delete("/chat/message-filters/{filter_id}")
async def delete_message_filter(filter_id: int, _: AdminCsrfSession) -> dict[str, object]:
    """删除一条消息过滤规则。"""
    if not await ChatAssistService().delete_message_filter(filter_id):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_FILTER_NOT_FOUND"})
    return {"data": {"id": filter_id, "deleted": True}}


@router.post("/chat/message-filters/batch-delete")
async def batch_delete_message_filters(
    body: MessageFilterBatchDeleteRequest, _: AdminCsrfSession
) -> dict[str, object]:
    """批量删除过滤规则。"""
    if any(filter_id <= 0 for filter_id in body.ids):
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_FILTER_REJECTED"})
    deleted = await ChatAssistService().batch_delete_message_filters(body.ids)
    return {"data": {"deleted": deleted}}


@router.get("/chat/buyer-notes")
async def buyer_note(
    _: AdminSession,
    account_id: str,
    buyer_id: str,
) -> dict[str, object]:
    """查看一个买家的备注(只给运营看)。"""
    try:
        row = await ChatAssistService().get_buyer_note(account_id=account_id, buyer_id=buyer_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}) from exc
    return {
        "data": None
        if row is None
        else {
            "account_id": row.account_id,
            "buyer_id": row.buyer_id,
            "note": row.note,
            "updated_at": format_local(row.updated_at) or None,
        }
    }


@router.put("/chat/buyer-notes")
async def save_buyer_note(
    body: BuyerNoteRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """写入或更新买家备注。"""
    try:
        row = await ChatAssistService().save_buyer_note(
            account_id=body.account_id, buyer_id=body.buyer_id, note=body.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "BUYER_NOTE_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="chat.buyer_note_saved",
                target=f"{body.account_id}/{body.buyer_id}",
                params={"length": len(row.note)},  # 备注正文不进审计
                result="success",
            )
        )
        await session.commit()
    return {"data": {"buyer_id": row.buyer_id}}


@router.get("/items/automation")
async def item_automation(
    _: AdminSession,
    account_id: str,
) -> dict[str, object]:
    """列出该账号带人工覆盖的商品(只读)。"""
    rows = await domain_items.list_automation_overrides(account_id)
    return {
        "data": [
            {
                "item_id": row.external_item_id,
                "title": row.title,
                "mode": row.automation_override,
                "note": row.automation_note,
                "by": row.automation_overridden_by,
                "at": format_local(row.automation_overridden_at) or None,
            }
            for row in rows
        ]
    }


@router.post("/items/automation")
async def set_item_automation(
    body: ItemAutomationRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """设置单品级人工覆盖;人工决定先于自动策略。"""
    mode = None if body.mode == "clear" else body.mode
    try:
        written = await domain_items.set_automation_override(
            body.account_id,
            body.item_id,
            mode=mode,
            note=body.note,
            actor="web-admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "ITEM_AUTOMATION_REJECTED"}) from exc
    if not written:
        raise HTTPException(status_code=404, detail={"code": "ITEM_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="item.automation_override_set",
                target=f"{body.account_id}/{body.item_id}",
                params={"mode": body.mode, "has_note": bool(body.note)},  # 备注不进审计正文
                result="success",
            )
        )
        await session.commit()
    return {"data": {"item_id": body.item_id, "mode": body.mode}}


@router.get("/attention")
async def attention(
    _: AdminSession,
    account_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    """待人工处理清单:结果不明、需要确认或需要重新验证的事项。"""
    items = await AttentionService().list_items(account_id=account_id, limit=limit)
    return {
        "data": [
            {
                "kind": item.kind,
                "target": item.target,
                "account_id": item.account_id,
                "state": item.state,
                "reason": item.reason,
                "next_command": item.next_command,
                "since": format_local(item.since) or None,
            }
            for item in items
        ]
    }


@router.post("/accounts/{account_id}/commands")
async def submit_account_command(
    account_id: str,
    body: WorkerCommandRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """Queue a daemon-owned account command; the Web process never owns the worker."""
    try:
        command = await worker_commands.submit(account_id, body.action, requested_by="web")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "COMMAND_REJECTED"}) from exc
    if command is None:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="worker_command_queued",
                target=account_id,
                params={"action": command.action, "command_id": command.command_id},
                result="queued",
            )
        )
        await session.commit()
    return {
        "data": {
            "command_id": command.command_id,
            "action": command.action,
            "status": command.status,
        }
    }


@router.get("/commands/{command_id}")
async def command_status(command_id: str, _: AdminSession) -> dict[str, object]:
    """Read queued-command progress without exposing daemon error details."""
    command = await worker_commands.get(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail={"code": "COMMAND_NOT_FOUND"})
    return {
        "data": {
            "command_id": command.command_id,
            "action": command.action,
            "status": command.status,
            "daemon_instance_id": command.daemon_instance_id,
            "requested_at": format_local(command.requested_at) or None,
            "claimed_at": format_local(command.claimed_at) or None,
            "completed_at": format_local(command.completed_at) or None,
        }
    }


@router.get("/runtime")
async def runtime(_: AdminSession) -> dict[str, object]:
    """Return daemon instance health without process-control capabilities."""
    instances = await runtime_daemon.list_instances()
    return {
        "data": [
            {
                "instance_id": instance.instance_id,
                "pid": instance.pid,
                "status": instance.status,
                "version": instance.version,
                "started_at": format_local(instance.started_at) or None,
                "last_heartbeat_at": format_local(instance.last_heartbeat_at) or None,
                "lease_until": format_local(instance.lease_until) or None,
                "stopped_at": format_local(instance.stopped_at) or None,
                "last_error": instance.last_error,
            }
            for instance in instances
        ]
    }


@router.get("/messages")
async def messages(
    _: AdminSession,
    account_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    chat_id: str | None = None,
) -> dict[str, object]:
    """List persisted messages for one account without exposing raw transport data."""
    rows = await domain_messages.list_recent(account_id=account_id, limit=limit, chat_id=chat_id)
    return {
        "data": [
            {
                "id": row.id,
                "chat_id": row.chat_id,
                "item_id": row.item_id,
                "direction": row.direction,
                "sender": row.sender_name or row.sender_id,
                "content_type": row.content_type,
                "content": row.content,
                # 买家发的图片:镜像里已经存了地址,之前一直没有回给界面。
                "image_url": row.image_url,
                "received_at": format_local(row.received_at) or None,
            }
            for row in rows
        ]
    }


@router.get("/conversations")
async def conversations(
    _: AdminSession,
    account_id: str,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    """List canonical conversations so the operator can see who owns auto-reply."""
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return {"data": []}
        rows = list(
            (
                await session.execute(
                    select(Conversation)
                    .where(Conversation.account_id == account.id)
                    .order_by(
                        Conversation.last_message_at.desc().nullslast(), Conversation.id.desc()
                    )
                    .limit(limit)
                )
            ).scalars()
        )
    return {
        "data": [
            {
                "chat_id": row.external_conversation_id,
                "buyer_id": row.buyer_id,
                "item_id": row.item_id,
                "last_message_at": format_local(row.last_message_at) or None,
                "unread_count": row.unread_count,
                "bot_state": row.bot_state,
                "takeover_state": row.takeover_state,
            }
            for row in rows
        ]
    }


@router.post("/conversations/{chat_id}/takeover")
async def set_conversation_takeover(
    chat_id: str,
    body: ConversationTakeoverRequest,
    account_id: str,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """Hand one conversation to a human, or return it to automated replies.

    Only the ownership flag changes. The endpoint never sends a message and
    never touches any other conversation of the same account.
    """
    if body.state not in {"human", "bot"}:
        raise HTTPException(status_code=400, detail={"code": "TAKEOVER_STATE_INVALID"})
    conversation = await domain_messages.set_takeover_state(
        account_id=account_id,
        chat_id=chat_id,
        state=body.state,  # type: ignore[arg-type]
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="conversation_takeover_changed",
                target=chat_id,
                params={"account_id": account_id, "takeover_state": conversation.takeover_state},
                result="ok",
            )
        )
        await session.commit()
    return {
        "data": {
            "chat_id": conversation.external_conversation_id,
            "takeover_state": conversation.takeover_state,
            "bot_state": conversation.bot_state,
        }
    }


@router.post("/conversations/{chat_id}/reply")
async def send_conversation_reply(
    chat_id: str,
    body: ConversationReplyRequest,
    account_id: str,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """Queue one operator-authored reply on the daemon that owns the account.

    Sending is a real external side effect, so it requires an explicit ``confirm``
    flag, an existing conversation, and a running Worker. The endpoint queues work
    only; it never opens its own WebSocket session alongside the daemon.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail={"code": "REPLY_CONFIRMATION_REQUIRED"})
    conversation = await domain_messages.get_conversation(account_id=account_id, chat_id=chat_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
    try:
        command = await worker_commands.submit_send_reply(
            account_id, chat_id=chat_id, text=body.text, requested_by="web"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "REPLY_REJECTED"}) from exc
    if command is None:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    digest = hashlib.sha256(body.text.strip().encode("utf-8")).hexdigest()
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="message.operator_reply_queued",
                target=chat_id,
                params={
                    "account_id": account_id,
                    "command_id": command.command_id,
                    "content_sha256": digest,
                    "content_length": len(body.text.strip()),
                },
                result="queued",
            )
        )
        await session.commit()
    return {"data": {"command_id": command.command_id, "status": command.status}}


@router.get("/orders")
async def orders(
    _: AdminSession,
    account_id: str,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    """List local seller-order mirrors; this endpoint never delivers content."""
    rows = await domain_orders.list_for_account(account_id, status=status, limit=limit)
    return {
        "data": [
            {
                "id": row.id,
                "order_id": row.order_id,
                "item_id": row.item_id,
                "item_title": row.item_title,
                "buyer_id": row.buyer_id,
                "buyer_name": row.buyer_name,
                "amount": row.amount,
                "quantity": row.quantity,
                "status": row.status,
                "placed_at": format_local(row.placed_at) or None,
                "paid_at": format_local(row.paid_at) or None,
                "delivered_at": format_local(row.delivered_at) or None,
                "delivery_state": "failed" if row.delivery_fail_reason else row.status,
            }
            for row in rows
        ]
    }


@router.get("/orders/{order_id}/details")
async def order_details(
    order_id: str, account_id: str, _: AdminSession, response: Response
) -> dict[str, object]:
    """实时订单详情;联系方式仅返回给已登录管理员,不写镜像。"""
    response.headers["Cache-Control"] = "no-store, private"
    return {"data": await XianyuOrderReadClient().fetch_details(account_id, order_id)}


@router.get("/orders/{order_id}/consign-info")
async def order_consign_info(
    order_id: str,
    account_id: str,
    _: AdminSession,
) -> dict[str, object]:
    """读取订单发货页信息,不改变平台订单状态。"""
    try:
        data = await XianyuOrderReadClient().fetch_consign_info(account_id, order_id)
    except (OrderReadError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"code": "ORDER_READ_FAILED"}) from exc
    return {"data": data}


@router.get("/orders/{order_id}/logistics")
async def order_logistics(
    order_id: str,
    account_id: str,
    _: AdminSession,
) -> dict[str, object]:
    """读取订单物流轨迹,不改变平台订单状态。"""
    try:
        data = await XianyuOrderReadClient().fetch_logistics(account_id, order_id)
    except (OrderReadError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"code": "ORDER_READ_FAILED"}) from exc
    return {"data": data}


@router.get("/orders/{order_id}/refund-record")
async def order_refund_record(
    order_id: str,
    account_id: str,
    _: AdminSession,
) -> dict[str, object]:
    """读取订单退款记录摘要,不改变平台订单状态。"""
    try:
        data = await XianyuOrderReadClient().fetch_refund_record(account_id, order_id)
    except (OrderReadError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"code": "ORDER_READ_FAILED"}) from exc
    return {"data": data}


@router.get("/items")
async def items(
    _: AdminSession,
    account_id: str,
    include_off_sale: bool = False,
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, object]:
    """List local item mirrors; reads no platform data and triggers no sync."""
    rows = await ItemQueryService().list_items(
        account_id,
        on_sale_only=not include_off_sale,
        limit=limit,
    )
    return {
        "data": [
            {
                "id": row.local_id,
                "item_id": row.external_item_id,
                "title": row.title,
                "main_image_url": row.main_image_url,
                "images": list(row.images),
                "price": None if row.price is None else str(row.price),
                "sold_count": row.sold_count,
                "has_multi_spec": row.has_multi_spec,
                "is_on_sale": row.is_on_sale,
                "last_synced_at": format_local(row.synced_at) or None,
            }
            for row in rows
        ]
    }


@router.get("/items/{account_id}/{item_id}/images")
async def item_images(
    account_id: str, item_id: str, response: Response, _: AdminSession, platform: bool = False
) -> dict[str, object]:
    """读取已知图片与规格图,可选只读平台图库;不触发商品同步或发布。"""
    row = await ItemQueryService().get_item(account_id, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "ITEM_NOT_FOUND"})
    platform_images: tuple[str, ...] = ()
    platform_error = None
    if platform:
        response.headers["Cache-Control"] = "no-store, private"
        try:
            facts = await ItemDetailService().fetch_facts(account_id, item_id)
            platform_images = facts.images or ()
        except (ItemDetailError, AccountCoolingError, ValueError):
            platform_error = "平台图库读取失败,当前显示本地已有图片。"
    return {
        "data": {
            "main_image_url": row.main_image_url,
            "images": list(row.images),
            "platform_images": list(platform_images),
            "platform_error": platform_error,
            "variant_images": [
                {
                    "label": " / ".join(f"{spec.name}: {spec.value}" for spec in variant.specs),
                    "url": variant.image_url,
                }
                for variant in row.variants
                if variant.image_url
            ],
        }
    }


@router.patch("/items/{account_id}/{item_id}")
async def update_local_item(
    account_id: str,
    item_id: str,
    body: LocalItemUpdateRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """修改本地商品目录字段,不发起平台商品编辑请求。"""
    if not body.model_fields_set:
        raise HTTPException(status_code=400, detail={"code": "NO_ITEM_CHANGES"})
    try:
        item = await domain_items.update_local_item(account_id, item_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "ITEM_UPDATE_REJECTED"}) from exc
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "ITEM_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="item.local.updated",
                target=item_id,
                params={"account_id": account_id, "fields": sorted(body.model_fields_set)},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"account_id": account_id, "item_id": item_id, "updated": True}}


@router.delete("/items/{account_id}/{item_id}")
async def archive_local_item(
    account_id: str,
    item_id: str,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """归档本地商品目录记录,不删除平台商品。"""
    if not await domain_items.archive_local_item(account_id, item_id):
        raise HTTPException(status_code=404, detail={"code": "ITEM_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="item.local.archived",
                target=item_id,
                params={"account_id": account_id},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"account_id": account_id, "item_id": item_id, "archived": True}}


@router.get("/inventory")
async def inventory(
    _: AdminSession,
    account_id: str | None = None,
) -> dict[str, object]:
    """Read card-stock counts only; card contents and consumption remain unavailable."""
    rows = await domain_cards.list_cards(account_id=account_id)
    return {
        "data": [
            {
                "id": row.id,
                "account_id": account_id,
                "name": row.name,
                "type": row.type,
                "total": row.total,
                "remaining": row.remaining,
                "enabled": row.enabled,
            }
            for row in rows
        ]
    }


@router.get("/delivery/jobs")
async def delivery_jobs(
    _: AdminSession,
    account_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """List delivery jobs for one account; reads state only, never delivers."""
    async with get_async_session() as session:
        rows = list(
            (
                await session.execute(
                    select(DeliveryJob)
                    .where(DeliveryJob.account_id == account_id)
                    .order_by(DeliveryJob.created_at.desc(), DeliveryJob.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )
    return {
        "data": [
            {
                "id": row.id,
                "order_id": row.order_id,
                "state": row.state,
                "required_quantity": row.required_quantity,
                "reservation_id": row.reservation_id,
                "item_id": row.item_id,
                "last_error": row.last_error,
                "created_at": format_local(row.created_at) or None,
                "completed_at": format_local(row.completed_at) or None,
            }
            for row in rows
        ]
    }


@router.post("/delivery/jobs/{job_id}/resolve")
async def resolve_delivery_job(
    job_id: int,
    body: DeliveryJobResolveRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """人工收尾一条结果不明的发货任务;不触发任何平台动作。

    与 CLI 同一条应用层路径:必须写明依据,且任务与库存一起收尾。这个接口只改本地事实,
    所以不需要平台的校准门禁;它也不会替运营核对平台,依据由操作人提供。
    """
    try:
        job = await DeliveryJobService().resolve_uncertain(
            job_id,
            delivered=body.delivered,
            note=body.note,
            resolved_by=body.resolved_by or "web",
            actor="web",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "RESOLVE_REJECTED"}) from exc
    return {
        "data": {
            "id": job.local_id,
            "state": job.state,
            "reservation_id": job.reservation_id,
        }
    }


@router.get("/delivery/api-configs")
async def delivery_api_configs(_: AdminSession) -> dict[str, object]:
    """List external-delivery configuration without returning template values or secrets."""
    configs = await ApiConfigService().list_configs()
    references = {ref for config in configs for ref in config.secret_refs}
    configured: dict[str, bool] = {}
    if references:
        vault = _api_secret_vault()
        states = await asyncio.gather(*(vault.configured(SecretRef(ref)) for ref in references))
        configured = dict(zip(references, states, strict=True))
    return {
        "data": [
            {
                "id": config.local_id,
                "name": config.name,
                "url": config.url,
                "method": config.method,
                "header_names": list(config.headers),
                "response_path": config.response_path,
                "timeout_seconds": config.timeout_seconds,
                "enabled": config.enabled,
                "description": config.description,
                "variables": list(config.variables),
                "secret_refs": list(config.secret_refs),
                "secrets_configured": all(configured.get(ref, False) for ref in config.secret_refs),
            }
            for config in configs
        ]
    }


@router.post("/delivery/api-configs")
async def upsert_delivery_api_config(
    body: ApiDeliveryConfigRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """Create or replace one structural API configuration; no outbound request is made."""
    service = ApiConfigService()
    try:
        existing = next(
            (config for config in await service.list_configs() if config.name == body.name.strip()),
            None,
        )
        values = body.model_dump()
        if existing is not None and existing.local_id is not None:
            config = await service.update(existing.local_id, **values)
            action = "delivery.api_config.updated"
        else:
            config = await service.create(**values)
            action = "delivery.api_config.created"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "API_CONFIG_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action=action,
                target=config.name,
                params={"config_id": config.local_id, "secret_refs": list(config.secret_refs)},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": config.local_id,
            "name": config.name,
            "variables": list(config.variables),
            "secret_refs": list(config.secret_refs),
        }
    }


@router.put("/delivery/api-configs/{config_id}")
async def update_delivery_api_config(
    config_id: int,
    body: ApiDeliveryConfigUpdateRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        config = await ApiConfigService().update(config_id, **body.model_dump(exclude_unset=True))
    except ValueError as exc:
        if "不存在" in str(exc):
            raise HTTPException(status_code=404, detail={"code": "API_CONFIG_NOT_FOUND"}) from exc
        raise HTTPException(status_code=400, detail={"code": "API_CONFIG_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.api_config.updated",
                target=config.name,
                params={"config_id": config_id, "secret_refs": list(config.secret_refs)},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": config.local_id,
            "name": config.name,
            "method": config.method,
            "variables": list(config.variables),
            "secret_refs": list(config.secret_refs),
        }
    }


@router.post("/delivery/api-configs/{config_id}/enabled")
async def set_delivery_api_config_enabled(
    config_id: int,
    body: ApiConfigEnabledRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        config = await ApiConfigService().set_enabled(config_id, enabled=body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "API_CONFIG_NOT_FOUND"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.api_config.enabled_changed",
                target=config.name,
                params={"config_id": config_id, "enabled": body.enabled},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": config.local_id, "enabled": config.enabled}}


@router.delete("/delivery/api-configs/{config_id}")
async def delete_delivery_api_config(
    config_id: int,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """删除发卡 API 结构配置;密钥引用本身由 SecretVault 独立管理。"""
    service = ApiConfigService()
    try:
        config = await service.get(config_id)
        await service.delete(config_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "API_CONFIG_NOT_FOUND"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.api_config.deleted",
                target=config.name,
                params={"config_id": config_id},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": config_id, "deleted": True}}


@router.post("/delivery/api-configs/{config_id}/test")
async def probe_delivery_api_config(
    config_id: int,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """试调用一次发卡 API:只验证能不能取到卡,不预留库存、不给买家发消息。

    响应只回掩码预览与长度:取到的是真实卡密资源,明文不经过管理接口。
    """
    try:
        result = await ApiConfigService().test_fetch(str(config_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "API_CONFIG_NOT_FOUND"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.api_config.tested",
                target=str(config_id),
                params={
                    "ok": result.content is not None,
                    "dispatched": result.dispatched,
                    "status_code": result.status_code,
                },
                result="success" if result.content is not None else "failed",
            )
        )
        await session.commit()
    return {
        "data": {
            "ok": result.content is not None,
            "dispatched": result.dispatched,
            "status_code": result.status_code,
            "preview": masked_preview(result.content) if result.content else None,
            "length": len(result.content) if result.content else 0,
            "error": result.error,
        }
    }


@router.get("/delivery/pools/{pool_id}/units")
async def delivery_pool_units(
    pool_id: int,
    _: AdminSession,
    state: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    """列出卡券池里的卡密单元;只回掩码预览与状态。"""
    try:
        units = await InventoryQueryService().list_units(pool_id, state=state or None, limit=limit)
        view = await InventoryQueryService().pool_detail(pool_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "POOL_NOT_FOUND"}) from exc
    return {
        "data": [
            {
                "unit_id": unit.unit_id,
                "state": unit.state,
                "preview": unit.preview,
                "reserved_by": unit.reserved_by,
                "reservation_id": unit.reservation_id,
            }
            for unit in units
        ],
        "counts": dict(view.counts),
    }


@router.post("/delivery/pools/{pool_id}/units")
async def import_delivery_pool_units(
    pool_id: int,
    body: CardUnitImportRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """把逐行卡密导入 data 池;重复行与空白行按现有规则跳过。"""
    try:
        summary = await InventoryImportService().import_units(pool_id, body.contents.splitlines())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "UNIT_IMPORT_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.pool.units_imported",
                target=str(pool_id),
                params={
                    "added": summary.added,
                    "skipped_duplicates": summary.skipped_duplicates,
                    "skipped_blank": summary.skipped_blank,
                },
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "added": summary.added,
            "skipped_duplicates": summary.skipped_duplicates,
            "skipped_blank": summary.skipped_blank,
        }
    }


@router.get("/delivery/templates")
async def delivery_templates(
    _: AdminSession,
    only_enabled: bool = False,
) -> dict[str, object]:
    """列出发货模板库(只回文案结构与变量,不含任何密钥)。"""
    rows = await DeliveryTemplateService().list_templates(only_enabled=only_enabled)
    return {
        "data": [
            {
                "id": row.local_id,
                "name": row.name,
                "content": row.content,
                "variables": list(row.variables),
                "enabled": row.enabled,
                "description": row.description,
            }
            for row in rows
        ]
    }


@router.post("/delivery/templates")
async def create_delivery_template(
    body: DeliveryTemplateRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """创建或按名称覆盖一个发货模板。"""
    service = DeliveryTemplateService()
    try:
        existing = next(
            (row for row in await service.list_templates() if row.name == body.name.strip()),
            None,
        )
        if existing is not None and existing.local_id is not None:
            template = await service.update(
                existing.local_id,
                content=body.content,
                description=body.description,
                enabled=body.enabled,
            )
            action = "delivery.template.updated"
        else:
            template = await service.create(
                name=body.name,
                content=body.content,
                description=body.description,
                enabled=body.enabled,
            )
            action = "delivery.template.created"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "TEMPLATE_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action=action,
                target=template.name,
                params={"template_id": template.local_id, "variables": list(template.variables)},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": template.local_id,
            "name": template.name,
            "variables": list(template.variables),
            "enabled": template.enabled,
        }
    }


@router.put("/delivery/templates/{template_id}")
async def update_delivery_template(
    template_id: int,
    body: DeliveryTemplateUpdateRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        template = await DeliveryTemplateService().update(
            template_id, **body.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        if "不存在" in str(exc):
            raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND"}) from exc
        raise HTTPException(status_code=400, detail={"code": "TEMPLATE_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.template.updated",
                target=template.name,
                params={"template_id": template_id, "variables": list(template.variables)},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": template.local_id,
            "name": template.name,
            "variables": list(template.variables),
            "enabled": template.enabled,
        }
    }


@router.post("/delivery/templates/{template_id}/enabled")
async def set_delivery_template_enabled(
    template_id: int,
    body: ApiConfigEnabledRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        template = await DeliveryTemplateService().set_enabled(template_id, enabled=body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "TEMPLATE_NOT_FOUND"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.template.enabled_changed",
                target=template.name,
                params={"template_id": template_id, "enabled": body.enabled},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": template.local_id, "enabled": template.enabled}}


@router.delete("/delivery/templates/{template_id}")
async def delete_delivery_template(
    template_id: int,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """删除未被启用策略引用的模板。"""
    try:
        await DeliveryTemplateService().delete(template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "TEMPLATE_DELETE_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.template.deleted",
                target=str(template_id),
                params={},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": template_id, "deleted": True}}


@router.get("/delivery/pools")
async def delivery_pools(
    _: AdminSession,
    account_id: str | None = None,
) -> dict[str, object]:
    views = await InventoryQueryService().list_pools()
    return {
        "data": [
            {
                "id": pool.local_id,
                "name": pool.name,
                "type": pool.type,
                "account_scope": pool.account_scope,
                "item_scope": pool.item_scope,
                "sku_scope": pool.sku_scope,
                "api_config_ref": pool.api_config_ref,
                "image_url": pool.image_url if pool.type == "image" else None,
                "delay_seconds": pool.delay_seconds,
                "enabled": pool.enabled,
                "available": view.available,
                "reserved": view.reserved,
                "uncertain": view.uncertain,
            }
            for view in views
            if (pool := view.pool).account_scope in {None, account_id}
        ]
    }


@router.post("/delivery/pools")
async def create_delivery_pool(
    body: InventoryPoolRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        pool = await InventoryService().create_pool(
            name=body.name,
            type_=body.type,
            account_scope=body.account_scope,
            item_scope=body.item_scope,
            sku_scope=body.sku_scope,
            api_config_ref=body.api_config_ref,
            content=body.content,
            image_url=body.image_url,
            delay_seconds=body.delay_seconds,
            enabled=body.enabled,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVENTORY_POOL_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.pool.created",
                target=pool.name,
                params={
                    "pool_id": pool.local_id,
                    "type": pool.type,
                    "account_scope": pool.account_scope,
                },
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": pool.local_id, "name": pool.name, "type": pool.type}}


@router.patch("/delivery/pools/{pool_id}")
async def update_delivery_pool(
    pool_id: int,
    body: InventoryPoolUpdateRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    if (
        body.name is None
        and body.description is None
        and body.content is None
        and body.image_url is None
        and body.account_scope is None
        and body.item_scope is None
        and body.sku_scope is None
        and body.delay_seconds is None
        and body.enabled is None
        and not body.clear_scope
    ):
        raise HTTPException(status_code=400, detail={"code": "NO_POOL_CHANGES"})
    try:
        pool = await InventoryService().update_pool(
            pool_id,
            name=body.name,
            description=body.description,
            content=body.content,
            image_url=body.image_url,
            account_scope=body.account_scope,
            item_scope=body.item_scope,
            sku_scope=body.sku_scope,
            delay_seconds=body.delay_seconds,
            enabled=body.enabled,
            clear_scope=body.clear_scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVENTORY_POOL_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.pool.updated",
                target=pool.name,
                params={"pool_id": pool.local_id, "fields": sorted(body.model_fields_set)},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": pool.local_id, "name": pool.name, "type": pool.type}}


@router.delete("/delivery/pools/{pool_id}")
async def delete_delivery_pool(
    pool_id: int,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        pool = await InventoryService().load(pool_id)
        await InventoryService().delete_pool(pool_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVENTORY_POOL_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.pool.deleted",
                target=pool.name,
                params={"pool_id": pool_id},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": pool_id, "deleted": True}}


@router.get("/delivery/policies")
async def delivery_policies(
    _: AdminSession,
    account_id: str | None = None,
) -> dict[str, object]:
    policies = await DeliveryPolicyService().list(account_id=account_id)
    return {
        "data": [
            {
                "id": policy.local_id,
                "name": policy.name,
                "inventory_pool_id": policy.inventory_pool_id,
                "account_id": policy.account_id,
                "item_id": policy.item_id,
                "sku_id": policy.sku_id,
                "level": policy.level,
                "delay_seconds": policy.delay_seconds,
                "priority": policy.priority,
                "enabled": policy.enabled,
                "platform_confirm": policy.platform_confirm,
            }
            for policy in policies
        ]
    }


@router.post("/delivery/policies")
async def create_delivery_policy(
    body: DeliveryPolicyRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        policy = await DeliveryPolicyService().create(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "DELIVERY_POLICY_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.policy.created",
                target=policy.name,
                params={
                    "policy_id": policy.local_id,
                    "pool_id": policy.inventory_pool_id,
                    "scope": policy.scope_key(),
                },
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": policy.local_id,
            "name": policy.name,
            "level": policy.level,
            "enabled": policy.enabled,
        }
    }


@router.patch("/delivery/policies/{policy_id}")
async def update_delivery_policy(
    policy_id: int,
    body: DeliveryPolicyUpdateRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    changes = body.model_dump(exclude_unset=True)
    changes.pop("clear_scope", None)
    if not changes and not body.clear_scope:
        raise HTTPException(status_code=400, detail={"code": "NO_POLICY_CHANGES"})
    try:
        policy = await DeliveryPolicyService().update(
            policy_id, clear_scope=body.clear_scope, **changes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "DELIVERY_POLICY_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.policy.updated",
                target=policy.name,
                params={"policy_id": policy_id, "fields": sorted(body.model_fields_set)},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": policy.local_id,
            "name": policy.name,
            "level": policy.level,
            "enabled": policy.enabled,
            "sku_id": policy.sku_id,
        }
    }


@router.post("/delivery/api-secrets")
async def create_delivery_api_secret(
    body: ApiSecretRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """Encrypt one secret and return only its opaque reference."""
    try:
        ref = await _api_secret_vault().put(
            SecretValue(body.value.get_secret_value()),
            metadata=SecretMetadata(
                {"purpose": "delivery_api", "label": body.label.strip() or "delivery_api"}
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "SECRET_VAULT_UNAVAILABLE"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.api_secret.created",
                target=ref.value,
                params={"label": body.label.strip() or "delivery_api"},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"ref": ref.value, "configured": True}}


@router.put("/delivery/api-secrets/{ref}")
async def rotate_delivery_api_secret(
    ref: str,
    body: ApiSecretRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """Rotate an encrypted secret in place, preserving all template references."""
    try:
        await _api_secret_vault().replace(
            SecretRef(ref), SecretValue(body.value.get_secret_value())
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "API_SECRET_NOT_FOUND"}) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "SECRET_VAULT_UNAVAILABLE"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.api_secret.rotated",
                target=ref,
                params={},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"ref": ref, "configured": True}}


@router.post("/delivery/plan")
async def delivery_plan(body: DeliveryPlanRequest, _: AdminSession) -> dict[str, object]:
    """Dry Run one delivery: says whether it would proceed and from which pool.

    This performs no reservation, no send, and no state change.
    """
    plan = await DeliveryExecutor().plan(
        DeliveryRequest(
            account_id=body.account_id,
            order_id=body.order_id,
            item_id=body.item_id,
            buyer_id=body.buyer_id,
            quantity=body.quantity or None,
        )
    )
    return {
        "data": {
            "allowed": plan.allowed,
            "reason": plan.reason,
            "order_status": plan.order_status,
            "guard_decision": plan.guard_decision,
            "policy_name": plan.policy_name,
            "policy_level": plan.policy_level,
            "pool_id": plan.pool_id,
            "quantity": plan.quantity,
            "delay_seconds": plan.delay_seconds,
            "platform_confirm": plan.platform_confirm,
            "hit_rules": list(plan.hit_rules),
        }
    }


@router.post("/delivery/run")
async def delivery_run(body: DeliveryRunRequest, _: AdminCsrfSession) -> dict[str, object]:
    """Queue one real delivery on the daemon that owns the account.

    Delivering sends card content to a buyer, so it requires an explicit
    confirm flag. The daemon runs the phase-6 executor against the live
    Worker, which keeps the reservation, attempt log and audit trail intact.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail={"code": "DELIVERY_CONFIRMATION_REQUIRED"})
    try:
        command = await worker_commands.submit_delivery(
            body.account_id,
            order_id=body.order_id,
            item_id=body.item_id,
            quantity=body.quantity,
            requested_by="web",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "DELIVERY_REJECTED"}) from exc
    if command is None:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="delivery.queued",
                target=body.order_id,
                params={
                    "account_id": body.account_id,
                    "command_id": command.command_id,
                    "item_id": body.item_id,
                },
                result="queued",
            )
        )
        await session.commit()
    return {"data": {"command_id": command.command_id, "status": command.status}}


@router.get("/rules")
async def rules(
    _: AdminSession,
    account_id: str | None = None,
) -> dict[str, object]:
    """Read reply-rule configuration without evaluating or sending a reply."""
    rows = await domain_rules.list_rules(account_id=account_id)
    return {
        "data": [
            {
                "id": row.id,
                "name": row.name,
                "type": row.type,
                "pattern": row.pattern,
                "priority": row.priority,
                "enabled": row.enabled,
                "reply_once": row.reply_once,
                # 配图只回"有没有"与地址本身(地址是运营自己填的公开 https 链接)。
                "reply_image_url": row.reply_image_url,
            }
            for row in rows
        ]
    }


@router.get("/platform-automation")
async def platform_automation(
    _: AdminSession,
    account_id: str,
) -> dict[str, object]:
    try:
        rows = await PlatformAutomationService().list(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}) from exc
    return {
        "data": [
            {
                "action": row.action,
                "enabled": row.enabled,
                "calibrated": row.calibrated,
                "calibration_approved_at": format_local(row.calibration_approved_at) or None,
                "calibration_approved_by": row.calibration_approved_by,
                "schedule_time": row.schedule_time,
                "delay_minutes": row.delay_minutes,
                "quiet_hours": row.quiet_hours,
            }
            for row in rows
        ]
    }


@router.get("/seller-features")
async def seller_features(_: AdminSession, account_id: str) -> dict[str, object]:
    """返回参考项目 seller-features 的只读投影。"""
    try:
        row = await PlatformAutomationService().seller_features(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}) from exc
    return {
        "data": {
            "account_id": row.account_id,
            "polish_enabled": row.polish_enabled,
            "request_flower_enabled": row.request_flower_enabled,
            "auto_rate_enabled": row.auto_rate_enabled,
            "auto_thanks_enabled": row.auto_thanks_enabled,
            "unsupported": list(row.unsupported),
        }
    }


@router.get("/platform-automation/runs")
async def platform_automation_runs(
    _: AdminSession,
    account_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    try:
        rows = await PlatformAutomationService().list_runs(account_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"}) from exc
    return {
        "data": [
            {
                "id": row.id,
                "action": row.action,
                "target_id": row.target_id,
                "status": row.status,
                "result": row.result,
                "started_at": format_local(row.started_at) or None,
                "finished_at": format_local(row.finished_at) or None,
            }
            for row in rows
        ]
    }


@router.post("/platform-automation/run-now")
async def platform_automation_run_now(
    body: RunNowRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """立刻排一次平台动作;要求 active + 已校准 + 已启用 + 非静默时段。"""
    settings = get_settings()
    try:
        work_id = await enqueue_immediate_run(
            account_id=body.account_id,
            action=body.action,
            active=settings.automation_mode == "active",
            timezone=settings.tz,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "RUN_NOW_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="platform_automation.run_now_queued",
                target=f"{body.account_id}:{body.action}",
                params={"work_id": work_id},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"work_id": work_id, "status": "queued"}}


@router.get("/item-lifecycle/plan")
async def item_lifecycle_plan(  # noqa: PLR0917 - 与 CLI 的 Dry Run 选项一一对应
    _: AdminSession,
    account_id: str,
    delist_after_days: int = Query(default=30, ge=1),
    delete_after_days: int = Query(default=0, ge=0),
    daily_limit: int = Query(default=0, ge=0),
    include_keyword: str | None = None,
    exclude_keyword: str | None = None,
    min_price: float = Query(default=0, ge=0),
    max_price: float = Query(default=0, ge=0),
) -> dict[str, object]:
    """生命周期 Dry Run:只读预览每个商品会被怎么处理(供 Agent 介入前判断)。"""
    plan = await ItemLifecycleService().plan(
        account_id,
        policy=lifecycle_policy(
            delist_after_days=delist_after_days,
            delete_after_days=delete_after_days,
            daily_limit=daily_limit,
        ),
        item_filter=lifecycle_item_filter(
            include_keywords=(include_keyword,) if include_keyword else (),
            exclude_keywords=(exclude_keyword,) if exclude_keyword else (),
            min_price=min_price,
            max_price=max_price,
        ),
    )
    return {
        "data": {
            "counts": plan.counts,
            "scanned": plan.scanned,
            "truncated": plan.truncated,
            "rows": [
                {
                    "item_id": row.item.external_item_id,
                    "action": row.decision.action,
                    "reason": row.decision.reason,
                }
                for row in plan.rows
            ],
        }
    }


@router.get("/accounts/{account_id}/profile")
async def account_profile(
    account_id: str, response: Response, _: AdminSession
) -> dict[str, object]:
    """只读卖家资料供头像展示;不更新昵称、不写操作审计。"""
    response.headers["Cache-Control"] = "no-store, private"
    if await domain_accounts.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    try:
        profile = await XianyuUserProfileClient().fetch_profile(account_id)
    except UserProfileError as exc:
        raise HTTPException(status_code=400, detail={"code": "PROFILE_READ_FAILED"}) from exc
    return {
        "data": {
            "account_id": account_id,
            "nickname": profile.nickname,
            "avatar_url": profile.avatar_url,
        }
    }


@router.post("/accounts/{account_id}/refresh-profile")
async def refresh_account_profile(
    account_id: str,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """从平台读取昵称/头像并写入本地镜像(只读接口,不改平台状态)。"""
    try:
        profile = await XianyuUserProfileClient().fetch_profile(account_id)
    except UserProfileError as exc:
        raise HTTPException(status_code=400, detail={"code": "PROFILE_REFRESH_FAILED"}) from exc
    if profile.nickname:
        await domain_accounts.set_nickname(account_id, profile.nickname)
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="account.profile_refreshed",
                target=account_id,
                params={"has_nickname": bool(profile.nickname)},  # 昵称正文不进审计
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "account_id": account_id,
            "nickname": profile.nickname,
            "avatar_url": profile.avatar_url,
        }
    }


@router.post("/platform-automation/runs/{run_id}/resolve")
async def resolve_platform_automation_run(
    run_id: int,
    body: AutomationRunResolveRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """人工收尾一条结果不明的执行事实;不会触发任何平台动作。"""
    try:
        row = await PlatformAutomationService().resolve_run(
            run_id, status=body.status, note=body.note, resolved_by="web-admin"
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "AUTOMATION_RUN_RESOLVE_REJECTED"}
        ) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="platform_automation.run_resolved",
                target=f"run:{run_id}",
                params={"status": row.status},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": row.id, "status": row.status, "result": row.result}}


@router.post("/platform-automation")
async def configure_platform_automation(
    account_id: str,
    body: PlatformAutomationRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        row = await PlatformAutomationService().configure(
            account_id,
            body.action,
            enabled=body.enabled,
            schedule_time=body.schedule_time,
            delay_minutes=body.delay_minutes,
            quiet_hours=body.quiet_hours,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "PLATFORM_AUTOMATION_REJECTED"}
        ) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="platform_automation.configured",
                target=f"{account_id}:{row.action}",
                params={
                    "enabled": row.enabled,
                    "schedule_time": row.schedule_time,
                    "delay_minutes": row.delay_minutes,
                    "quiet_hours": row.quiet_hours,
                },
                result="success",
            )
        )
        await session.commit()
    return {"data": {"action": row.action, "enabled": row.enabled, "calibrated": row.calibrated}}


@router.post("/platform-automation/calibration")
async def approve_platform_automation_calibration(
    account_id: str,
    body: AutomationCalibrationRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    if not body.confirm:
        raise HTTPException(status_code=400, detail={"code": "CALIBRATION_CONFIRMATION_REQUIRED"})
    try:
        row = await PlatformAutomationService().approve_calibration(
            account_id, body.action, approved_by="web-admin"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CALIBRATION_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="platform_automation.calibration_approved",
                target=f"{account_id}:{row.action}",
                params={},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"action": row.action, "enabled": row.enabled, "calibrated": True}}


@router.get("/logs/summary")
async def logs_summary(
    _: AdminSession,
    recent: int = Query(default=20, ge=0, le=200),
) -> dict[str, object]:
    """daemon 日志的健康摘要:各级别行数 + 最近的告警行(再次脱敏后返回)。

    只读本机日志文件;返回内容经过 `redact_text`,并且只挑出 WARNING 及以上,避免把完整日志
    变成管理接口的输出。
    """
    path = get_settings().daemon_log_path
    if not path.exists():
        return {"data": {"exists": False, "path": str(path), "counts": {}, "recent": []}}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    counts = summarize_log_levels(lines)
    interesting = [
        redact_text(line)
        for line in lines
        if any(level in line for level in ("WARNING", "ERROR", "CRITICAL"))
    ]
    return {
        "data": {
            "exists": True,
            "path": str(path),
            "total_lines": len(lines),
            "counts": counts,
            "recent": interesting[-recent:] if recent else [],
        }
    }


@router.get("/risk-events")
async def risk_events(
    _: AdminSession,
    account_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """风险相关事件:风控熔断的打开/解除,以及账号进入需人工验证的审计。"""
    statement = (
        select(AuditLog)
        .where(
            AuditLog.action.like("ws_user_validate%")
            | (AuditLog.action == "ws_validation_recovery_probe")
        )
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    if account_id is not None:
        statement = statement.where(AuditLog.target == account_id)
    async with get_async_session() as session:
        rows = list((await session.scalars(statement)).all())
    return {
        "data": [
            {
                "id": row.id,
                "action": row.action,
                "target": row.target,
                "result": row.result,
                "created_at": format_local(row.created_at) or None,
            }
            for row in rows
        ]
    }


@router.get("/audit")
async def audit(
    _: AdminSession,
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    """Return an audit-safe projection; parameters and error text stay server-side."""
    async with get_async_session() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
                )
            ).scalars()
        )
    return {
        "data": [
            {
                "id": row.id,
                "actor": row.actor,
                "action": row.action,
                "target": row.target,
                "result": row.result,
                "created_at": format_local(row.created_at) or None,
            }
            for row in rows
        ]
    }


@router.get("/notify/channels")
async def notification_channels(_: AdminSession) -> dict[str, object]:
    """列出通知渠道;地址只回掩码,密钥只回 SecretVault 引用名。"""
    service = NotificationChannelService()
    rows = await service.list_channels()
    data = []
    for row in rows:
        channel_id = int(row.local_id or 0)
        data.append(
            {
                "id": row.local_id,
                "name": row.name,
                "kind": row.kind,
                "target_masked": row.masked_target(),
                "secret_ref": row.secret_ref,
                "events": list(row.events),
                "enabled": row.enabled,
                "description": row.description,
                "accounts": await service.bound_accounts(channel_id),
            }
        )
    return {"data": data}


@router.post("/notify/channels")
async def create_notification_channel(
    body: NotificationChannelRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """按名称新建或覆盖一条通知渠道。"""
    service = NotificationChannelService()
    events = tuple(body.events)
    try:
        existing = await service.list_channels()
        match = next((row for row in existing if row.name == body.name.strip()), None)
        if match is not None and match.local_id is not None:
            channel = await service.update(
                match.local_id,
                target=body.target,
                secret_ref=body.secret_ref or "",
                events=events,
                enabled=body.enabled,
                description=body.description,
            )
            action = "notify.channel.updated"
        else:
            channel = await service.create(
                name=body.name,
                kind=body.kind,
                target=body.target,
                secret_ref=body.secret_ref,
                events=events,
                enabled=body.enabled,
                description=body.description,
            )
            action = "notify.channel.created"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CHANNEL_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action=action,
                target=channel.name,
                # 审计只记结构与"有没有密钥",地址正文与密钥引用都不进审计。
                params={
                    "channel_id": channel.local_id,
                    "kind": channel.kind,
                    "events": list(channel.events),
                    "has_secret": channel.secret_ref is not None,
                },
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": channel.local_id,
            "name": channel.name,
            "kind": channel.kind,
            "enabled": channel.enabled,
            "events": list(channel.events),
        }
    }


@router.put("/notify/channels/{channel_id}")
async def update_notification_channel(
    channel_id: int,
    body: NotificationChannelUpdateRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """按参考项目提供通知渠道的显式更新入口。"""
    try:
        channel = await NotificationChannelService().update(
            channel_id,
            name=body.name,
            target=body.target,
            secret_ref=body.secret_ref,
            events=tuple(body.events) if body.events is not None else None,
            enabled=body.enabled,
            description=body.description,
        )
    except ValueError as exc:
        if "不存在" in str(exc):
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"}) from exc
        raise HTTPException(status_code=400, detail={"code": "CHANNEL_REJECTED"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="notify.channel.updated",
                target=channel.name,
                params={
                    "channel_id": channel.local_id,
                    "events": list(channel.events),
                    "has_secret": channel.secret_ref is not None,
                },
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {
            "id": channel.local_id,
            "name": channel.name,
            "kind": channel.kind,
            "enabled": channel.enabled,
            "events": list(channel.events),
        }
    }


@router.post("/notify/channels/{channel_id}/enabled")
async def set_notification_channel_enabled(
    channel_id: int,
    body: ApiConfigEnabledRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        channel = await NotificationChannelService().set_enabled(channel_id, enabled=body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="notify.channel.enabled_changed",
                target=channel.name,
                params={"channel_id": channel_id, "enabled": body.enabled},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": channel.local_id, "enabled": channel.enabled}}


@router.post("/notify/channels/{channel_id}/bindings")
async def bind_notification_channel(
    channel_id: int,
    body: NotificationBindingRequest,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """把渠道绑定到账号;绑过账号的渠道只收这些账号的事件。"""
    service = NotificationChannelService()
    try:
        await service.get(channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"}) from exc
    bound = await service.bind(channel_id, body.account_id)
    if not bound:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="notify.channel.bound",
                target=body.account_id,
                params={"channel_id": channel_id},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {"channel_id": channel_id, "accounts": await service.bound_accounts(channel_id)}
    }


@router.delete("/notify/channels/{channel_id}/bindings/{account_id}")
async def unbind_notification_channel(
    channel_id: int,
    account_id: str,
    _: AdminCsrfSession,
) -> dict[str, object]:
    service = NotificationChannelService()
    removed = await service.unbind(channel_id, account_id)
    if not removed:
        raise HTTPException(status_code=404, detail={"code": "BINDING_NOT_FOUND"})
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="notify.channel.unbound",
                target=account_id,
                params={"channel_id": channel_id},
                result="success",
            )
        )
        await session.commit()
    return {
        "data": {"channel_id": channel_id, "accounts": await service.bound_accounts(channel_id)}
    }


@router.post("/notify/channels/{channel_id}/test")
async def send_notification_channel_test(
    channel_id: int,
    _: AdminCsrfSession,
) -> dict[str, object]:
    """发一条测试消息;只回成功与失败原因,不回地址与密钥。"""
    try:
        result = await NotificationChannelService().test_send(channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"}) from exc
    if not result.ok:
        return {"data": {"ok": False, "error": result.error or "UNKNOWN"}}
    return {"data": {"ok": True, "error": None}}


@router.delete("/notify/channels/{channel_id}")
async def delete_notification_channel(
    channel_id: int,
    _: AdminCsrfSession,
) -> dict[str, object]:
    try:
        await NotificationChannelService().delete(channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"}) from exc
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="web",
                action="notify.channel.deleted",
                target=str(channel_id),
                params={},
                result="success",
            )
        )
        await session.commit()
    return {"data": {"id": channel_id, "deleted": True}}


@router.get("/notify/messages")
async def notification_messages(
    _: AdminSession,
    limit: int = Query(default=50, ge=1, le=200),
    only_failed: bool = False,
) -> dict[str, object]:
    """最近的发送事实;失败也记一条,便于运维发现渠道坏掉。"""
    async with get_async_session() as session:
        statement = select(AlertDispatch).order_by(AlertDispatch.id.desc()).limit(limit)
        if only_failed:
            statement = statement.where(AlertDispatch.ok.is_(False))
        rows = list((await session.scalars(statement)).all())
    return {
        "data": [
            {
                "id": row.id,
                "channel_id": row.channel_id,
                "event": row.kind,
                "target": row.target,
                "state": row.state,
                "ok": row.ok,
                "error": row.error,
                "created_at": format_local(row.created_at) or None,
            }
            for row in rows
        ]
    }
