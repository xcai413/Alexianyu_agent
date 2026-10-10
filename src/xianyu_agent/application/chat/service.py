"""快捷回复与买家备注的应用层(对照 Helper 的 chat/quick-replies、chat/buyer-notes)。

这两个功能只服务人工客服:它们不会被自动回复引擎读取,因此不会因为运营写了一句备注而改变
对买家说的话。买家备注把 buyer_id 固定在账号维度,避免不同账号下的同名买家串号。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from xianyu_agent.db import Account, BuyerNote, MessageFilterRule, QuickReply, get_async_session

MAX_QUICK_REPLY_TITLE = 64
MAX_QUICK_REPLY_CONTENT = 2000
MAX_BUYER_NOTE = 2000
MESSAGE_FILTER_TYPES = {"skip_reply", "skip_notify"}


@dataclass(frozen=True, slots=True)
class QuickReplyView:
    id: int
    account_id: str | None
    title: str
    content: str
    category: str
    sort_order: int
    enabled: bool
    use_count: int


@dataclass(frozen=True, slots=True)
class BuyerNoteView:
    account_id: str
    buyer_id: str
    note: str
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class MessageFilterView:
    id: int
    account_id: str
    keyword: str
    filter_type: str
    enabled: bool


class ChatAssistService:
    """Create and list the two human-facing chat helpers."""

    async def list_message_filters(
        self, *, account_id: str | None = None, filter_type: str | None = None
    ) -> list[MessageFilterView]:
        normalized_type = self._filter_type(filter_type) if filter_type else None
        async with get_async_session() as session:
            statement = select(MessageFilterRule, Account.account_id).join(Account).order_by(
                MessageFilterRule.created_at.desc(), MessageFilterRule.id.desc()
            )
            if account_id:
                statement = statement.where(Account.account_id == account_id.strip())
            if normalized_type:
                statement = statement.where(MessageFilterRule.filter_type == normalized_type)
            rows = (await session.execute(statement)).all()
        return [
            MessageFilterView(
                id=int(rule.id), account_id=account, keyword=rule.keyword,
                filter_type=rule.filter_type, enabled=bool(rule.enabled)
            )
            for rule, account in rows
        ]

    async def create_message_filter(
        self, *, account_id: str, keyword: str, filter_type: str, enabled: bool = True
    ) -> MessageFilterView:
        account_text = account_id.strip()
        word = keyword.strip()
        if not account_text:
            raise ValueError("账号不能为空")
        if not word or len(word) > 255:
            raise ValueError("过滤关键词不能为空且不能超过 255 个字符")
        normalized_type = self._filter_type(filter_type)
        async with get_async_session() as session:
            account = await session.scalar(select(Account).where(Account.account_id == account_text))
            if account is None:
                raise ValueError(f"账号 {account_text} 不存在")
            existing = await session.scalar(
                select(MessageFilterRule).where(
                    MessageFilterRule.account_id == account.id,
                    MessageFilterRule.keyword == word,
                    MessageFilterRule.filter_type == normalized_type,
                )
            )
            if existing is not None:
                raise ValueError("相同账号、关键词和类型的过滤规则已存在")
            row = MessageFilterRule(
                account_id=account.id, keyword=word, filter_type=normalized_type, enabled=enabled
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return MessageFilterView(int(row.id), account_text, word, normalized_type, bool(row.enabled))

    async def set_message_filter_enabled(self, filter_id: int, enabled: bool) -> bool:
        async with get_async_session() as session:
            row = await session.get(MessageFilterRule, filter_id)
            if row is None:
                return False
            row.enabled = enabled
            await session.commit()
            return True

    async def update_message_filter(
        self,
        filter_id: int,
        *,
        keyword: str | None = None,
        filter_type: str | None = None,
        enabled: bool | None = None,
    ) -> MessageFilterView | None:
        """更新规则并返回更新后的视图;账号归属不可通过此入口改变。"""
        word = keyword.strip() if keyword is not None else None
        if word is not None and (not word or len(word) > 255):
            raise ValueError("过滤关键词不能为空且不能超过 255 个字符")
        normalized_type = self._filter_type(filter_type) if filter_type else None
        async with get_async_session() as session:
            row = await session.get(MessageFilterRule, filter_id)
            if row is None:
                return None
            next_word = word if word is not None else row.keyword
            next_type = normalized_type or row.filter_type
            duplicate = await session.scalar(
                select(MessageFilterRule).where(
                    MessageFilterRule.id != filter_id,
                    MessageFilterRule.account_id == row.account_id,
                    MessageFilterRule.keyword == next_word,
                    MessageFilterRule.filter_type == next_type,
                )
            )
            if duplicate is not None:
                raise ValueError("相同账号、关键词和类型的过滤规则已存在")
            row.keyword = next_word
            row.filter_type = next_type
            if enabled is not None:
                row.enabled = enabled
            await session.commit()
            account = await session.scalar(select(Account).where(Account.id == row.account_id))
            return MessageFilterView(
                int(row.id), account.account_id if account else "", row.keyword,
                row.filter_type, bool(row.enabled)
            )

    async def batch_create_message_filters(
        self, *, account_id: str, keywords: list[str], filter_type: str, enabled: bool = True
    ) -> tuple[int, int]:
        """按参考项目语义批量创建,重复规则计入 skipped。"""
        seen: set[str] = set()
        created = skipped = 0
        for keyword in keywords:
            normalized = keyword.strip()
            if not normalized or normalized.casefold() in seen:
                skipped += 1
                continue
            seen.add(normalized.casefold())
            try:
                await self.create_message_filter(
                    account_id=account_id, keyword=normalized,
                    filter_type=filter_type, enabled=enabled
                )
            except ValueError as exc:
                if "已存在" not in str(exc):
                    raise
                skipped += 1
            else:
                created += 1
        if not seen:
            raise ValueError("至少提供一个过滤关键词")
        return created, skipped

    async def batch_delete_message_filters(self, filter_ids: list[int]) -> int:
        deleted = 0
        for filter_id in dict.fromkeys(filter_ids):
            deleted += int(await self.delete_message_filter(filter_id))
        return deleted

    async def delete_message_filter(self, filter_id: int) -> bool:
        async with get_async_session() as session, session.begin():
            row = await session.get(MessageFilterRule, filter_id)
            if row is None:
                return False
            await session.delete(row)
        return True

    async def enabled_reply_filter_keywords(self, account_id: str) -> list[str]:
        rows = await self.list_message_filters(account_id=account_id, filter_type="skip_reply")
        return [row.keyword for row in rows if row.enabled]

    @staticmethod
    def _filter_type(value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in MESSAGE_FILTER_TYPES:
            raise ValueError("过滤类型必须是 skip_reply 或 skip_notify")
        return normalized

    async def add_quick_reply(
        self,
        *,
        title: str,
        content: str,
        account_id: str | None = None,
        sort_order: int = 0,
        category: str = "默认",
    ) -> QuickReplyView:
        resolved_title = title.strip()
        resolved_content = content.strip()
        if not resolved_title:
            msg = "快捷回复标题不能为空"
            raise ValueError(msg)
        if len(resolved_title) > MAX_QUICK_REPLY_TITLE:
            msg = f"快捷回复标题不能超过 {MAX_QUICK_REPLY_TITLE} 个字符"
            raise ValueError(msg)
        if not resolved_content:
            msg = "快捷回复内容不能为空"
            raise ValueError(msg)
        if len(resolved_content) > MAX_QUICK_REPLY_CONTENT:
            msg = f"快捷回复内容不能超过 {MAX_QUICK_REPLY_CONTENT} 个字符"
            raise ValueError(msg)
        if sort_order < 0:
            msg = "排序值不能为负数"
            raise ValueError(msg)
        resolved_category = category.strip() or "默认"
        if len(resolved_category) > 64:
            raise ValueError("快捷回复分类不能超过 64 个字符")
        async with get_async_session() as session:
            account_pk = await self._account_pk(session, account_id)
            existing = await session.scalar(
                self._quick_reply_lookup(account_pk, resolved_title)
            )
            if existing is not None:
                msg = f"快捷回复 {resolved_title} 在该范围内已存在"
                raise ValueError(msg)
            row = QuickReply(
                account_id=account_pk,
                title=resolved_title,
                content=resolved_content,
                category=resolved_category,
                sort_order=sort_order,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                msg = f"快捷回复 {resolved_title} 在该范围内已存在"
                raise ValueError(msg) from exc
            await session.refresh(row)
            view = self._quick_reply_view(account_id, row)
        return view

    async def list_quick_replies(
        self, *, account_id: str | None = None, include_disabled: bool = True
    ) -> list[QuickReplyView]:
        async with get_async_session() as session:
            account_pk = await self._account_pk(session, account_id)
            statement = select(QuickReply).order_by(
                QuickReply.category.asc(), QuickReply.sort_order.asc(), QuickReply.id.asc()
            )
            if account_id is None:
                statement = statement.where(QuickReply.account_id.is_(None))
            else:
                # 账号视图同时给出账号专属与全局模板:人工客服需要两者都能用。
                statement = statement.where(
                    (QuickReply.account_id == account_pk) | (QuickReply.account_id.is_(None))
                )
            if not include_disabled:
                statement = statement.where(QuickReply.enabled.is_(True))
            rows = list((await session.scalars(statement)).all())
            return [
                self._quick_reply_view(account_id if row.account_id else None, row)
                for row in rows
            ]

    async def update_quick_reply(
        self,
        reply_id: int,
        *,
        title: str | None = None,
        content: str | None = None,
        category: str | None = None,
        sort_order: int | None = None,
        enabled: bool | None = None,
    ) -> QuickReplyView | None:
        """更新快捷短语元数据,不触发任何平台发送。"""
        resolved_title = title.strip() if title is not None else None
        resolved_content = content.strip() if content is not None else None
        resolved_category = category.strip() if category is not None else None
        if resolved_title is not None and not resolved_title:
            raise ValueError("快捷回复标题不能为空")
        if resolved_content is not None and not resolved_content:
            raise ValueError("快捷回复内容不能为空")
        if resolved_category is not None and not resolved_category:
            resolved_category = "默认"
        if sort_order is not None and sort_order < 0:
            raise ValueError("排序值不能为负数")
        if resolved_category is not None and len(resolved_category) > 64:
            raise ValueError("快捷回复分类不能超过 64 个字符")
        async with get_async_session() as session:
            row = await session.get(QuickReply, reply_id)
            if row is None:
                return None
            if resolved_title is not None:
                duplicate_statement = select(QuickReply).where(
                    QuickReply.account_id == row.account_id,
                    QuickReply.title == resolved_title,
                    QuickReply.id != reply_id,
                ).limit(1)
                duplicate = await session.scalar(duplicate_statement)
                if duplicate is not None:
                    raise ValueError(f"快捷回复 {resolved_title} 在该范围内已存在")
                row.title = resolved_title
            if resolved_content is not None:
                row.content = resolved_content
            if resolved_category is not None:
                row.category = resolved_category
            if sort_order is not None:
                row.sort_order = sort_order
            if enabled is not None:
                row.enabled = enabled
            await session.commit()
            await session.refresh(row)
            account_id = await session.scalar(
                select(Account.account_id).where(Account.id == row.account_id)
            )
            return self._quick_reply_view(account_id, row)

    async def use_quick_reply(self, reply_id: int) -> bool:
        """记录人工插入一次;不代表已经向平台发送。"""
        async with get_async_session() as session:
            row = await session.get(QuickReply, reply_id)
            if row is None:
                return False
            row.use_count += 1
            await session.commit()
        return True

    async def delete_quick_reply(self, reply_id: int) -> bool:
        async with get_async_session() as session, session.begin():
            result = await session.execute(delete(QuickReply).where(QuickReply.id == reply_id))
        return bool(result.rowcount)

    async def save_buyer_note(
        self, *, account_id: str, buyer_id: str, note: str
    ) -> BuyerNoteView:
        resolved_buyer = buyer_id.strip()
        resolved_note = note.strip()
        if not resolved_buyer:
            msg = "buyer_id 不能为空"
            raise ValueError(msg)
        if not resolved_note:
            msg = "备注内容不能为空(清空备注请用 delete)"
            raise ValueError(msg)
        if len(resolved_note) > MAX_BUYER_NOTE:
            msg = f"备注不能超过 {MAX_BUYER_NOTE} 个字符"
            raise ValueError(msg)
        async with get_async_session() as session, session.begin():
            account_pk = await self._account_pk(session, account_id)
            row = await session.scalar(
                select(BuyerNote)
                .where(
                    BuyerNote.account_id == account_pk,
                    BuyerNote.buyer_id == resolved_buyer,
                )
                .limit(1)
            )
            if row is None:
                row = BuyerNote(
                    account_id=account_pk, buyer_id=resolved_buyer, note=resolved_note
                )
                session.add(row)
            else:
                row.note = resolved_note
            await session.flush()
            # updated_at 是 server_default/onupdate,flush 后必须显式刷新再读,
            # 否则属性访问会在 greenlet 之外触发惰性 IO。
            await session.refresh(row)
            view = BuyerNoteView(
                account_id=account_id,
                buyer_id=row.buyer_id,
                note=row.note,
                updated_at=row.updated_at,
            )
        return view

    async def get_buyer_note(self, *, account_id: str, buyer_id: str) -> BuyerNoteView | None:
        async with get_async_session() as session:
            account_pk = await self._account_pk(session, account_id)
            row = await session.scalar(
                select(BuyerNote)
                .where(
                    BuyerNote.account_id == account_pk,
                    BuyerNote.buyer_id == buyer_id.strip(),
                )
                .limit(1)
            )
        if row is None:
            return None
        return BuyerNoteView(
            account_id=account_id,
            buyer_id=row.buyer_id,
            note=row.note,
            updated_at=row.updated_at,
        )

    async def delete_buyer_note(self, *, account_id: str, buyer_id: str) -> bool:
        async with get_async_session() as session, session.begin():
            account_pk = await self._account_pk(session, account_id)
            result = await session.execute(
                delete(BuyerNote).where(
                    BuyerNote.account_id == account_pk,
                    BuyerNote.buyer_id == buyer_id.strip(),
                )
            )
        return bool(result.rowcount)

    @staticmethod
    async def _account_pk(session, account_id: str | None) -> int | None:
        """全局模板用 NULL 表示;传了账号就必须真实存在。"""
        if account_id is None:
            return None
        text = account_id.strip()
        if not text:
            return None
        row = await session.scalar(select(Account).where(Account.account_id == text).limit(1))
        if row is None:
            msg = f"账号 {text} 不存在"
            raise ValueError(msg)
        return int(row.id)

    @staticmethod
    def _quick_reply_lookup(account_pk: int | None, title: str):
        """同名判重:全局模板(account_id 为 NULL)在 SQL 里不会触发唯一约束,必须显式查。"""
        statement = select(QuickReply).where(QuickReply.title == title)
        if account_pk is None:
            return statement.where(QuickReply.account_id.is_(None)).limit(1)
        return statement.where(QuickReply.account_id == account_pk).limit(1)

    @staticmethod
    def _quick_reply_view(account_id: str | None, row: QuickReply) -> QuickReplyView:
        return QuickReplyView(
            id=int(row.id),
            account_id=None if row.account_id is None else account_id,
            title=row.title,
            content=row.content,
            category=row.category,
            sort_order=int(row.sort_order),
            enabled=bool(row.enabled),
            use_count=int(row.use_count),
        )
