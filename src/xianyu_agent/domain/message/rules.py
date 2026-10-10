"""Reply rule domain: CRUD, matching, and reply-log recording."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select

from xianyu_agent.db import (
    Account,
    AuditLog,
    Item,
    Message,
    ReplyLog,
    ReplyRule,
    get_async_session,
)
from xianyu_agent.db.models import RuleType

# Default priority: lower value = higher priority.
DEFAULT_PRIORITY = 100


async def create_rule(
    name: str,
    type_: str,
    pattern: str,
    reply_text: str,
    *,
    account_id: str | None = None,
    item_pk: int | None = None,
    priority: int = DEFAULT_PRIORITY,
    enabled: bool = True,
    reply_once: bool = False,
    reply_image_url: str | None = None,
) -> ReplyRule:
    """Create a rule. account_id=None means global (applies to all accounts)."""
    if type_ not in {t.value for t in RuleType}:
        msg = f"type_ 必须是 {[t.value for t in RuleType]},got {type_!r}"
        raise ValueError(msg)
    async with get_async_session() as session:
        account = None
        if account_id is not None:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                msg = f"账号 {account_id} 不存在"
                raise ValueError(msg)
        item = None
        if item_pk is not None:
            item = (
                await session.execute(select(Item).where(Item.id == item_pk).limit(1))
            ).scalar_one_or_none()
            if item is None:
                raise ValueError(f"商品镜像 #{item_pk} 不存在")
            if account is not None and account.id != item.account_id:
                raise ValueError(f"商品镜像 #{item_pk} 不属于账号 {account_id}")
            account = await session.get(Account, item.account_id)
        row = ReplyRule(
            account_id=account.id if account else None,
            item_pk=item.id if item else None,
            name=name,
            type=type_,
            pattern=pattern,
            reply_text=reply_text,
            reply_image_url=reply_image_url,
            priority=priority,
            enabled=enabled,
            reply_once=reply_once,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def list_rules(
    *, account_id: str | None = None, item_pk: int | None = None
) -> Sequence[ReplyRule]:
    """List rules; account_id filters to that account's rules (excludes global)."""
    async with get_async_session() as session:
        stmt = select(ReplyRule)
        if account_id is not None:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return []
            stmt = stmt.where(ReplyRule.account_id == account.id)
        if item_pk is not None:
            stmt = stmt.where(ReplyRule.item_pk == item_pk)
        stmt = stmt.order_by(ReplyRule.priority.asc(), ReplyRule.id.asc())
        return list((await session.execute(stmt)).scalars().all())


async def get_rule(rule_id: int) -> ReplyRule | None:
    async with get_async_session() as session:
        return (
            await session.execute(select(ReplyRule).where(ReplyRule.id == rule_id).limit(1))
        ).scalar_one_or_none()


async def set_rule_enabled(rule_id: int, enabled: bool) -> bool:
    async with get_async_session() as session:
        row = (
            await session.execute(select(ReplyRule).where(ReplyRule.id == rule_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.enabled = enabled
        await session.commit()
        return True


async def delete_rule(rule_id: int) -> bool:
    async with get_async_session() as session:
        row = (
            await session.execute(select(ReplyRule).where(ReplyRule.id == rule_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


def rule_matches(rule: ReplyRule, content: str) -> bool:
    """Match a single rule against message content."""
    if not rule.enabled:
        return False
    if rule.type == RuleType.KEYWORD.value:
        return rule.pattern.lower() in content.lower()
    if rule.type == RuleType.REGEX.value:
        try:
            return re.search(rule.pattern, content) is not None
        except re.error:
            return False
    return rule.type == RuleType.DEFAULT.value


async def match_for_account(
    account_id: str, content: str, *, item_id: str | None = None
) -> list[ReplyRule]:
    """Return enabled rules matching content, best first.

    Order: account-specific rules first (by priority), then global rules
    (by priority). Only the first match is used by the reply engine.
    """
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return []
        item = None
        if item_id:
            item = (
                await session.execute(
                    select(Item)
                    .where(Item.account_id == account.id, Item.item_id == item_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        stmt = select(ReplyRule).where(
            ReplyRule.enabled.is_(True),
            (ReplyRule.account_id == account.id) | ReplyRule.account_id.is_(None),
        )
        if item is None:
            stmt = stmt.where(ReplyRule.item_pk.is_(None))
        else:
            stmt = stmt.where((ReplyRule.item_pk == item.id) | ReplyRule.item_pk.is_(None))
        rules = list((await session.execute(stmt)).scalars().all())
    rules.sort(
        key=lambda rule: (
            _scope_rank(rule, item.id if item else None),
            _match_rank(rule),
            rule.priority,
            rule.id,
        )
    )
    return [rule for rule in rules if rule_matches(rule, content)]


def _scope_rank(rule: ReplyRule, item_pk: int | None) -> int:
    if item_pk is not None and rule.item_pk == item_pk:
        return 0
    if rule.account_id is not None:
        return 1
    return 2


def _match_rank(rule: ReplyRule) -> int:
    """Keep default rules as fallbacks within the same scope."""
    return 1 if rule.type == RuleType.DEFAULT.value else 0


async def record_hit(rule_id: int) -> None:
    async with get_async_session() as session:
        row = (
            await session.execute(select(ReplyRule).where(ReplyRule.id == rule_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return
        row.hit_count += 1
        row.last_hit_at = datetime.now(UTC)
        await session.commit()


async def replied_once_in_conversation(rule_id: int, message_id: int | None) -> bool:
    """Return whether this rule already succeeded in the inbound message's conversation."""
    if message_id is None:
        return False
    async with get_async_session() as session:
        message = await session.get(Message, message_id)
        if message is None:
            return False
        stmt = (
            select(ReplyLog.id)
            .join(Message, ReplyLog.message_id == Message.id)
            .where(ReplyLog.rule_id == rule_id)
            .where(ReplyLog.success.is_(True))
            .where(Message.conversation_id == message.conversation_id)
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None


async def record_reply_log(
    *,
    account_id: str,
    message_id: int | None,
    rule_id: int,
    sent_text: str | None,
    success: bool,
    error: str | None = None,
    source: str = "rule",
) -> ReplyLog | None:
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return None
        row = ReplyLog(
            account_id=account.id,
            message_id=message_id,
            rule_id=rule_id,
            sent_text=sent_text,
            success=success,
            error=error,
            source=source,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def recent_reply_logs(*, limit: int = 20) -> Sequence[ReplyLog]:
    async with get_async_session() as session:
        stmt = select(ReplyLog).order_by(ReplyLog.sent_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())


@dataclass(frozen=True)
class ReplyLogRecord:
    """一条回复投递记录的脱敏投影。

    `sent_text` 只在这里变成"长度 + 前 8 位预览":这条记录唯一会被看到的场合是运营在终端里排查
    "默认回复为什么没再发",完整正文没有必须回显的理由。
    """

    id: int
    rule_id: int | None
    rule_name: str | None
    conversation_id: int | None
    success: bool
    source: str
    sent_at: datetime | None
    text_length: int
    text_preview: str | None
    has_image: bool
    error: str | None


def _mask_text(value: str | None) -> tuple[int, str | None]:
    text = (value or "").strip()
    if not text:
        return 0, None
    return len(text), (text[:8] + "…") if len(text) > 8 else text


async def list_reply_logs(
    account_id: str,
    *,
    rule_id: int | None = None,
    limit: int = 50,
) -> list[ReplyLogRecord]:
    """列出某账号的回复投递记录(对照 Helper 的默认回复记录视图)。

    这份记录是 `reply_once` 的判重依据,所以它同时是"为什么没再回"的答案来源。
    """
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            msg = f"账号 {account_id} 不存在"
            raise ValueError(msg)
        stmt = (
            select(ReplyLog, ReplyRule.name, Message.conversation_id)
            .outerjoin(ReplyRule, ReplyRule.id == ReplyLog.rule_id)
            .outerjoin(Message, Message.id == ReplyLog.message_id)
            .where(ReplyLog.account_id == account.id)
            .order_by(ReplyLog.sent_at.desc(), ReplyLog.id.desc())
            .limit(limit)
        )
        if rule_id is not None:
            stmt = stmt.where(ReplyLog.rule_id == rule_id)
        rows = (await session.execute(stmt)).all()
    records: list[ReplyLogRecord] = []
    for log, rule_name, conversation_id in rows:
        length, preview = _mask_text(log.sent_text)
        records.append(
            ReplyLogRecord(
                id=int(log.id),
                rule_id=log.rule_id,
                rule_name=rule_name,
                conversation_id=conversation_id,
                success=bool(log.success),
                source=log.source,
                sent_at=log.sent_at,
                text_length=length,
                text_preview=preview,
                has_image=bool(log.sent_image_url),
                error=log.error,
            )
        )
    return records


async def clear_reply_logs(
    account_id: str,
    *,
    rule_id: int | None = None,
    actor: str = "cli",
) -> int:
    """清空某账号的回复投递记录,返回删除条数(对照 Helper 的 `clear-records`)。

    为什么需要它:`reply_once` 规则靠这份记录判重,校准或测试时用过一次之后,同一个会话就再也
    不会触发——运营需要一个明确的重置入口,而不是去删库。这里**只删去重记录**,不改规则、不删
    消息、不删审计:回复决策的审计留在 `audit_logs`,清记录不会把历史抹掉,清空动作本身也写一条审计。
    """
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            msg = f"账号 {account_id} 不存在"
            raise ValueError(msg)
        stmt = delete(ReplyLog).where(ReplyLog.account_id == account.id)
        if rule_id is not None:
            stmt = stmt.where(ReplyLog.rule_id == rule_id)
        result = await session.execute(stmt)
        removed = int(result.rowcount or 0)
        session.add(
            AuditLog(
                actor=actor,
                action="reply_records_cleared",
                target=account_id,
                params={"rule_id": rule_id, "removed": removed},
                result="ok",
            )
        )
        await session.commit()
    return removed
