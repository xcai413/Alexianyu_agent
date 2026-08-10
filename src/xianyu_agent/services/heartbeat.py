"""Heartbeat & maintenance helpers.

- purge_old_messages(): delete message rows older than a retention window.
  Keeps chat history bounded; runs on demand (CLI) and periodically inside
  the pool foreground loop.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from xianyu_agent.db import Message, get_async_session

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_HOURS = 24


async def purge_old_messages(*, older_than_hours: float = DEFAULT_RETENTION_HOURS) -> int:
    """Delete messages received before `now - older_than_hours`. Returns count."""
    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    async with get_async_session() as session:
        result = await session.execute(delete(Message).where(Message.received_at < cutoff))
        await session.commit()
        deleted = result.rowcount or 0
        if deleted:
            logger.info("purged %d messages older than %sh", deleted, older_than_hours)
        return deleted
