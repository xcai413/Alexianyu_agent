"""In-process fan-out adapter for durable outbox events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from xianyu_agent.application.ports.outbox import OutboxRecord

EventHandler = Callable[[OutboxRecord], Awaitable[None]]


async def _invoke(handler: EventHandler, event: OutboxRecord) -> Exception | None:
    try:
        await handler(event)
    except Exception as exc:
        return exc
    return None


class InProcessEventBus:
    """Publish an event to registered handlers in registration order.

    Every registered handler gets one attempt per publish call. Handler failures
    are propagated only after fan-out completes so one failing consumer cannot
    permanently starve later consumers while the durable outbox record remains
    retryable.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._handlers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

    async def publish(self, event: OutboxRecord) -> None:
        first_error: Exception | None = None
        for handler in tuple(self._handlers.get(event.event_type, ())):
            error = await _invoke(handler, event)
            if first_error is None and error is not None:
                first_error = error
        if first_error is not None:
            raise first_error
