"""Dashboard endpoints backed by server-side read models."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from xianyu_agent.application.analytics import AnalyticsService
from xianyu_agent.db import Account, Item, Message, Order, get_async_session
from xianyu_agent.interfaces.web.auth.router import require_admin
from xianyu_agent.utils.time_utils import format_local

router = APIRouter(prefix="/api/v1/dashboard", tags=["web-dashboard"])
AdminSession = Annotated[dict[str, object], Depends(require_admin)]


@router.get("/summary")
async def summary(_: AdminSession) -> dict[str, object]:
    """Return one server-side operational summary; browser never aggregates raw lists."""
    async with get_async_session() as session:
        result = await session.execute(
            select(
                select(func.count(Account.id)).scalar_subquery(),
                select(func.count(Account.id)).where(Account.status == "online").scalar_subquery(),
                select(func.count(Message.id))
                .where(Message.direction == "inbound")
                .scalar_subquery(),
                select(func.count(Order.id)).scalar_subquery(),
                select(func.count(Item.id)).where(Item.is_on_sale.is_(True)).scalar_subquery(),
            )
        )
        accounts, online_accounts, inbound_messages, orders, items = result.one()
    return {
        "data": {
            "accounts": accounts,
            "online_accounts": online_accounts,
            "inbound_messages": inbound_messages,
            "orders": orders,
            "on_sale_items": items,
        }
    }


@router.get("/analytics")
async def analytics(
    _: AdminSession,
    account_id: str | None = None,
    days: str = Query(default="1,7,30"),
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, object]:
    """运营分析:窗口指标、自动化健康与镜像水位(只读)。"""
    try:
        windows = tuple(int(part) for part in days.split(",") if part.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_WINDOW"}) from exc
    try:
        report = await AnalyticsService().report(
            account_id=account_id or None,
            windows=windows,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "ANALYTICS_REJECTED"}) from exc
    return {
        "data": {
            "data_quality": {
                "orders_missing_paid_at": report.orders_missing_paid_at,
                "orders_missing_delivered_at": report.orders_missing_delivered_at,
            },
            "date_range": {
                "start": report.range_start.isoformat(),
                "end": report.range_end.isoformat(),
            }
            if report.range_start and report.range_end
            else None,
            "windows": [
                {
                    "days": window.days,
                    "orders": window.orders,
                    "paid_orders": window.paid_orders,
                    "delivered_orders": window.delivered_orders,
                    "amount": window.amount,
                    "inbound_messages": window.inbound_messages,
                    "outbound_messages": window.outbound_messages,
                    "new_items": window.new_items,
                }
                for window in report.windows
            ],
            "automation": {
                "jobs_completed": report.automation.jobs_completed,
                "jobs_failed": report.automation.jobs_failed,
                "jobs_waiting_human": report.automation.jobs_waiting_human,
                "automation_completed": report.automation.automation_completed,
                "automation_uncertain": report.automation.automation_uncertain,
            },
            "revenue_daily": [
                {"date": row.date.isoformat(), "amount": row.amount, "paid_orders": row.paid_orders}
                for row in report.revenue_daily
            ],
            "on_sale_items": report.on_sale_items,
            "attention_items": report.attention_items,
            "last_item_sync_at": format_local(report.last_item_sync_at) or None,
            "last_order_sync_at": format_local(report.last_order_sync_at) or None,
        }
    }
