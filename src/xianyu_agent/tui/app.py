"""DashboardApp: Textual 驾驶舱。

布局:顶栏 + 四区网格(账号池 / 消息流 / 订单 / 卡密)+ 页脚快捷键。
数据:每 REFRESH_S 轮询 SQLite 刷新四区。
按键:q 退出;! 紧急模式(暂停自动操作标记,写 audit_logs);r 手动刷新。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid
from textual.widgets import Footer, Header, Static

from xianyu_agent.db import AuditLog, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.services.guardrails import recent_guardrail_events
from xianyu_agent.tui.widgets import AccountsPanel, CardsPanel, MessagesPanel, OrdersPanel

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 2.0


class DashboardApp(App):
    TITLE = "闲鱼运营驾驶舱"
    CSS_PATH = "styles.tcss"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "退出"),
        Binding("!", "toggle_emergency", "紧急模式"),
        Binding("r", "refresh_now", "刷新"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.emergency = False
        self._status: Static | None = None
        self._risk: Static | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self._status = Static("", id="status-bar")
        yield self._status
        self._risk = Static("", id="risk-bar")
        yield self._risk
        yield Grid(
            AccountsPanel(),
            MessagesPanel(),
            OrdersPanel(),
            CardsPanel(),
            id="dashboard-grid",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(REFRESH_SECONDS, self.refresh_dashboard)
        await self.refresh_dashboard()

    async def refresh_dashboard(self) -> None:
        """Query all four panels and update the status line."""
        try:
            accounts = await domain_accounts.list_accounts()
            account_ids = [a.account_id for a in accounts]
            on_sale_total = 0
            for account_id in account_ids:
                on_sale_total += len(
                    await domain_items.list_items(account_id, on_sale_only=True)
                )
            await self.query_one(AccountsPanel).reload()
            await self.query_one(MessagesPanel).reload(account_ids)
            await self.query_one(OrdersPanel).reload(account_ids)
            await self.query_one(CardsPanel).reload(account_ids)
            now = datetime.now(UTC).strftime("%H:%M:%S")
            mode = "紧急暂停" if self.emergency else "正常"
            self._status.update(
                f"账号 {len(account_ids)}  |  在售商品 {on_sale_total}  |  "
                f"模式: {mode}  |  刷新: {now}"
            )
            events = await recent_guardrail_events(limit=5)
            if events:
                latest = events[0]
                self._risk.update(
                    f"[red]风险: {latest['account_id']} ({latest['rule']}) "
                    f"{latest['detail']} @ {latest['at']}[/red]"
                )
            else:
                self._risk.update("")
        except Exception as exc:
            logger.warning("dashboard refresh failed: %s", exc)
            self._status.update(f"刷新失败: {exc}")

    async def action_toggle_emergency(self) -> None:
        self.emergency = not self.emergency
        action = "emergency_pause" if self.emergency else "emergency_resume"
        try:
            async with get_async_session() as session:
                session.add(
                    AuditLog(
                        actor="tui",
                        action=action,
                        target="dashboard",
                        result="ok",
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("audit write failed: %s", exc)
        await self.refresh_dashboard()

    async def action_refresh_now(self) -> None:
        await self.refresh_dashboard()
