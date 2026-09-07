"""Application idempotency contract for at-least-once consumers."""

from __future__ import annotations

from typing import Protocol

from xianyu_agent.foundation.identifiers import IdempotencyKey


class IdempotencyStore(Protocol):
    """Reserve a consumer-scoped key inside the caller's transaction.

    A successful claim must be committed atomically with the consumer's business
    effects. If that transaction rolls back, the key becomes claimable again.
    """

    async def claim(self, consumer: str, key: IdempotencyKey, /) -> bool: ...
