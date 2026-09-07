"""Event publication port used by the transactional outbox dispatcher."""

from __future__ import annotations

from typing import Protocol

from .outbox import OutboxRecord


class EventBus(Protocol):
    """Publish one durable event envelope to downstream consumers."""

    async def publish(self, event: OutboxRecord) -> None: ...
