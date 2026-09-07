"""In-process fan-out adapter for durable outbox events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from xianyu_agent.application.ports.outbox import OutboxRecord

EventHandler = Callable[[OutboxRecord], Awaitable[None]]


class InProcessEventBus:
    """Publish an event to registered handlers in registration order.

    Handler failures are propagated so the transactional outbox dispatcher can
    release/retry the durable record instead of falsely marking it published.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._handlers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

    async def publish(self, event: OutboxRecord) -> None:
        for handler in tuple(self._handlers.get(event.event_type, ())):
            await handler(event)
