"""Reply rule domain: CRUD, matching, and reply-log recording."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from xianyu_agent.db import Account, ReplyLog, ReplyRule, get_async_session
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
    priority: int = DEFAULT_PRIORITY,
    enabled: bool = True,
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
        row = ReplyRule(
            account_id=account.id if account else None,
            name=name,
            type=type_,
            pattern=pattern,
            reply_text=reply_text,
            reply_image_url=reply_image_url,
            priority=priority,
            enabled=enabled,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def list_rules(*, account_id: str | None = None) -> Sequence[ReplyRule]:
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


async def match_for_account(account_id: str, content: str) -> list[ReplyRule]:
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
        stmt = (
            select(ReplyRule)
            .where(ReplyRule.enabled.is_(True))
            .where((ReplyRule.account_id == account.id) | (ReplyRule.account_id.is_(None)))
            .order_by(ReplyRule.priority.asc(), ReplyRule.id.asc())
        )
        rules = list((await session.execute(stmt)).scalars().all())
    return [r for r in rules if rule_matches(r, content)]


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
