"""Append-only audit evidence for reply decisions without message content."""

from __future__ import annotations

import logging

from xianyu_agent.db import AuditLog, get_async_session

ACTION = "message.reply_decision"
logger = logging.getLogger(__name__)


async def record_reply_decision(
    *,
    account_id: str,
    message_id: int | None,
    outcome: str,
    reason: str | None = None,
    source: str | None = None,
    rule_id: int | None = None,
) -> None:
    """Write decision metadata; never persist buyer content in this audit row."""
    params: dict[str, str | int] = {"outcome": outcome}
    if message_id is not None:
        params["message_id"] = message_id
    if reason is not None:
        params["reason"] = reason
    if source is not None:
        params["source"] = source
    if rule_id is not None:
        params["rule_id"] = rule_id
    try:
        async with get_async_session() as session:
            session.add(
                AuditLog(
                    actor="system",
                    action=ACTION,
                    target=account_id,
                    params=params,
                    result=outcome,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("reply decision audit write failed account=%s", account_id)
