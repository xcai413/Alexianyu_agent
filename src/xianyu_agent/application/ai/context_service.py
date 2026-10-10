"""Build bounded, time-windowed conversation context for AI requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xianyu_agent.config import get_settings
from xianyu_agent.domain.ai import AIConversationContext
from xianyu_agent.domain.item import items as domain_items
from xianyu_agent.domain.message import messages as domain_messages


class ContextService:
    """Build chronological text context without including the current inbound message."""

    def __init__(
        self,
        *,
        max_messages: int | None = None,
        max_age_seconds: int | None = None,
    ) -> None:
        settings = get_settings()
        self._max_messages = (
            settings.ai_context_max_messages if max_messages is None else max_messages
        )
        self._max_age_seconds = (
            settings.ai_context_max_age_seconds if max_age_seconds is None else max_age_seconds
        )
        if self._max_messages < 0 or self._max_age_seconds <= 0:
            raise ValueError("上下文条数不能为负数且时间窗口必须为正数")

    async def build(
        self,
        *,
        account_id: str,
        chat_id: str,
        current_message_id: int | None,
        item_id: str | None,
        now: datetime | None = None,
    ) -> AIConversationContext:
        """Return bounded message history and a local item-mirror fact block."""
        item_facts = await self._item_facts(account_id, item_id)
        if self._max_messages == 0:
            return AIConversationContext(messages=(), item_facts=item_facts)
        observed_at = now or datetime.now(UTC)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        since = observed_at - timedelta(seconds=self._max_age_seconds)
        rows = await domain_messages.list_recent(
            account_id=account_id,
            chat_id=chat_id,
            since=since,
            limit=self._max_messages + 1,
        )
        text = [
            row.content
            for row in reversed(rows)
            if row.id != current_message_id and row.content and row.content.strip()
        ]
        return AIConversationContext(
            messages=tuple(text[-self._max_messages :]), item_facts=item_facts
        )

    @staticmethod
    async def _item_facts(account_id: str, item_id: str | None) -> str | None:
        if not item_id:
            return None
        item = await domain_items.get_item_by_external_id(account_id, item_id)
        if item is None:
            return None
        availability = "在售" if item.is_on_sale else "已下架"
        price = item.price or "未同步"
        return (
            "商品事实 (本会话唯一的商品事实来源):\n"
            f"- 标题: {item.title}\n- 价格: {price}\n- 状态: {availability}"
        )
