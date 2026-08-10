"""Dashboard widgets: read-only panels backed by SQLite.

Each panel exposes `async def reload()` that queries the domain layer and
refreshes its DataTable. The app calls reload() on a timer.
"""

from __future__ import annotations

from textual.widgets import DataTable, Static

from xianyu_agent.domain import (
    cards as domain_cards,
    messages as domain_messages,
    orders as domain_orders,
)
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.utils.time_utils import to_local


class AccountsPanel(DataTable):
    """账号池状态:worker 状态 + 心跳。"""

    def __init__(self) -> None:
        super().__init__(id="accounts-panel")
        self.cursor_type = "row"

    async def on_mount(self) -> None:
        self.add_columns("账号", "启用", "worker", "db", "重连", "最后心跳", "错误")

    async def reload(self) -> None:
        rows = await AccountPool().status()
        self.clear()
        for r in rows:
            self.add_row(
                r["account_id"],
                "Y" if r["enabled"] else "N",
                r["worker_state"],
                r["db_status"],
                str(r["reconnect_attempts"]),
                (r["last_heartbeat_at"] or "-")[:19],
                (r["last_error"] or "-")[:30],
                key=r["account_id"],
            )


class MessagesPanel(DataTable):
    """最近消息流。"""

    def __init__(self) -> None:
        super().__init__(id="messages-panel")
        self.cursor_type = "row"

    async def on_mount(self) -> None:
        self.add_columns("时间", "账号", "方向", "chat", "发送者", "内容")

    async def reload(self, accounts: list[str]) -> None:
        self.clear()
        for acc in accounts:
            rows = await domain_messages.list_recent(account_id=acc, limit=8)
            for m in rows:
                ts = to_local(m.received_at).strftime("%H:%M:%S") if m.received_at else "-"
                self.add_row(
                    ts,
                    acc,
                    m.direction,
                    (m.chat_id or "-")[:14],
                    (m.sender_name or m.sender_id or "-")[:14],
                    (m.content or "")[:40],
                )


class OrdersPanel(DataTable):
    """最近订单。"""

    def __init__(self) -> None:
        super().__init__(id="orders-panel")
        self.cursor_type = "row"

    async def on_mount(self) -> None:
        self.add_columns("订单", "账号", "商品", "金额", "状态", "发货内容")

    async def reload(self, accounts: list[str]) -> None:
        self.clear()
        for acc in accounts:
            rows = await domain_orders.list_for_account(acc, limit=5)
            for o in rows:
                self.add_row(
                    o.order_id,
                    acc,
                    (o.item_title or "-")[:16],
                    str(o.amount),
                    o.status,
                    (o.delivery_content or "-")[:24],
                )


class CardsPanel(DataTable):
    """卡密库存。"""

    def __init__(self) -> None:
        super().__init__(id="cards-panel")
        self.cursor_type = "row"

    async def on_mount(self) -> None:
        self.add_columns("卡", "账号", "类型", "总数", "剩余", "启用")

    async def reload(self, accounts: list[str]) -> None:
        self.clear()
        for acc in accounts:
            rows = await domain_cards.list_cards(account_id=acc)
            for c in rows:
                self.add_row(
                    c.name,
                    acc,
                    c.type,
                    str(c.total),
                    str(c.remaining),
                    "Y" if c.enabled else "N",
                )


class StatusBar(Static):
    """顶部状态条:账号数 / 紧急模式 / 最后刷新。"""
