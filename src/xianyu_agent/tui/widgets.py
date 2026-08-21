"""Dashboard widgets: read-only panels backed by SQLite.

Each panel exposes `async def reload()` that queries the domain layer and
refreshes its DataTable. The app calls reload() on a timer.
"""

from __future__ import annotations

from textual.widgets import DataTable, Static

from xianyu_agent.domain import (
    cards as domain_cards,
    items as domain_items,
    messages as domain_messages,
    orders as domain_orders,
)
from xianyu_agent.services.observability import RuntimeSnapshot, build_runtime_snapshot
from xianyu_agent.utils.time_utils import to_local


class AccountsPanel(DataTable):
    """账号池状态:worker 状态 + 心跳。"""

    def __init__(self) -> None:
        super().__init__(id="accounts-panel")
        self.cursor_type = "row"

    async def on_mount(self) -> None:
        self.add_columns("账号", "启用", "期望", "实际", "一致", "心跳(s)", "错误")

    async def reload(self, snapshot: RuntimeSnapshot | None = None) -> None:
        runtime = snapshot or await build_runtime_snapshot()
        self.clear()
        for row in runtime.accounts:
            self.add_row(
                row.account_id,
                "Y" if row.enabled else "N",
                row.desired_state,
                row.actual_state,
                "Y" if row.aligned else "N",
                f"{row.heartbeat_age_s:.0f}" if row.heartbeat_age_s is not None else "-",
                (row.last_error or row.issue or "-")[:30],
                key=row.account_id,
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


class ItemsPanel(DataTable):
    """在售商品明细:本地只读镜像。"""

    def __init__(self) -> None:
        super().__init__(id="items-panel")
        self.cursor_type = "row"

    async def on_mount(self) -> None:
        self.add_columns("账号", "标题", "价格", "已售", "商品 ID", "最后同步")

    async def reload(self, accounts: list[str]) -> int:
        """刷新全部账号的在售镜像并返回汇总数量。"""
        self.clear()
        total = 0
        for account_id in accounts:
            rows = await domain_items.list_items(account_id, on_sale_only=True)
            sales = await domain_orders.sales_by_item(account_id)
            total += len(rows)
            for item in rows:
                synced_at = to_local(item.last_synced_at).strftime("%m-%d %H:%M")
                sale = sales.get(item.item_id)
                self.add_row(
                    account_id,
                    item.title[:42],
                    item.price or "-",
                    str(sale.sold_quantity) if sale is not None else "0",
                    item.item_id,
                    synced_at,
                    key=f"{account_id}:{item.id}",
                )
        return total


class StatusBar(Static):
    """顶部状态条:账号数 / 紧急模式 / 最后刷新。"""
