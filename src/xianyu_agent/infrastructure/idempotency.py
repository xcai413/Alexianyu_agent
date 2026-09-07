"""SQLAlchemy-backed consumer idempotency store."""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.foundation.identifiers import IdempotencyKey
from xianyu_agent.infrastructure.database.models.idempotency import ConsumerInbox

_MAX_CONSUMER_LENGTH = 128


class SqlAlchemyIdempotencyStore:
    """Reserve consumer-scoped keys in the caller's existing DB transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, consumer: str, key: IdempotencyKey, /) -> bool:
        consumer = consumer.strip()
        if not consumer:
            raise ValueError("consumer must not be empty")
        if len(consumer) > _MAX_CONSUMER_LENGTH:
            raise ValueError(f"consumer must not exceed {_MAX_CONSUMER_LENGTH} characters")

        record = ConsumerInbox(
            consumer=consumer,
            key_hash=sha256(key.value.encode("utf-8")).hexdigest(),
        )
        try:
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError:
            return False
        return True
