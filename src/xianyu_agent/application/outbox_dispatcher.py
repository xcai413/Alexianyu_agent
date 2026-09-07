"""At-least-once dispatcher for durable transactional-outbox events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .ports.event_bus import EventBus
from .ports.outbox import OutboxRecord
from .ports.unit_of_work import UnitOfWorkFactory

Clock = Callable[[], datetime]


class DispatchOutcome(StrEnum):
    """Outcome for one candidate selected from the pending outbox."""

    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Summary for one bounded dispatcher pass."""

    selected: int
    published: int
    failed: int
    skipped: int


class OutboxDispatcher:
    """Publish pending events and mark them durable only after publication succeeds.

    Publication happens before ``published_at`` is committed. Therefore a crash
    after the event bus accepts an event but before the database commit can cause
    the event to be delivered again on the next pass. That duplicate-delivery
    window is intentional and provides at-least-once rather than at-most-once
    semantics.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        event_bus: EventBus,
        *,
        batch_size: int = 100,
        clock: Clock | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._uow_factory = uow_factory
        self._event_bus = event_bus
        self._batch_size = batch_size
        self._clock = clock or _utcnow

    async def dispatch_once(self) -> DispatchResult:
        """Dispatch at most ``batch_size`` currently pending events."""
        pending = await self._pending_batch()
        published = 0
        failed = 0
        skipped = 0

        for candidate in pending:
            outcome = await self._dispatch_one(candidate)
            if outcome is DispatchOutcome.PUBLISHED:
                published += 1
            elif outcome is DispatchOutcome.FAILED:
                failed += 1
            else:
                skipped += 1

        return DispatchResult(
            selected=len(pending),
            published=published,
            failed=failed,
            skipped=skipped,
        )

    async def _pending_batch(self) -> tuple[OutboxRecord, ...]:
        async with self._uow_factory() as uow:
            return tuple(await uow.outbox.list_pending(limit=self._batch_size))

    async def _dispatch_one(self, candidate: OutboxRecord) -> DispatchOutcome:
        current = await self._reload_pending(candidate.event_id)
        if current is None:
            return DispatchOutcome.SKIPPED

        try:
            await self._event_bus.publish(current)
        except Exception as exc:
            await self._record_failure(current.event_id, exc)
            return DispatchOutcome.FAILED

        await self._mark_published(current.event_id)
        return DispatchOutcome.PUBLISHED

    async def _reload_pending(self, event_id: str) -> OutboxRecord | None:
        async with self._uow_factory() as uow:
            current = await uow.outbox.get_by_event_id(event_id)
        if current is None or current.published_at is not None:
            return None
        return current

    async def _record_failure(self, event_id: str, exc: Exception) -> None:
        async with self._uow_factory() as uow:
            current = await uow.outbox.get_by_event_id(event_id)
            if current is None or current.published_at is not None:
                return
            changed = await uow.outbox.record_failure(event_id, error=_safe_error(exc))
            if changed:
                await uow.commit()

    async def _mark_published(self, event_id: str) -> None:
        async with self._uow_factory() as uow:
            current = await uow.outbox.get_by_event_id(event_id)
            if current is None or current.published_at is not None:
                return
            changed = await uow.outbox.mark_published(event_id, published_at=self._clock())
            if changed:
                await uow.commit()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_error(exc: Exception) -> str:
    """Persist only an exception class so transport credentials cannot leak."""
    return type(exc).__name__
