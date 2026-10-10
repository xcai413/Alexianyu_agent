"""At-least-once dispatcher for durable transactional-outbox events."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

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
        owner: str | None = None,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if lease_duration <= timedelta(0):
            msg = "lease_duration must be positive"
            raise ValueError(msg)
        self._uow_factory = uow_factory
        self._event_bus = event_bus
        self._batch_size = batch_size
        self._clock = clock or _utcnow
        self._owner = owner or f"dispatcher-{uuid4().hex[:8]}"
        self._lease_duration = lease_duration

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
            claimed = await uow.outbox.claim_pending(
                owner=self._owner,
                lease_duration=self._lease_duration,
                limit=self._batch_size,
                now=self._clock(),
            )
            if claimed:
                # 抢占必须先落库,其他副本才会看到租约;否则两个副本会同时发布同一事件。
                await uow.commit()
            return tuple(claimed)

    async def _dispatch_one(self, candidate: OutboxRecord) -> DispatchOutcome:
        current = await self._reload_pending(candidate.event_id)
        if current is None:
            await self._release_claim(candidate.event_id, candidate.lease_token)
            return DispatchOutcome.SKIPPED

        try:
            await self._event_bus.publish(current)
        except Exception as exc:
            await self._record_failure(current.event_id, exc, candidate.lease_token)
            return DispatchOutcome.FAILED

        await self._mark_published(current.event_id, candidate.lease_token)
        return DispatchOutcome.PUBLISHED

    async def _reload_pending(self, event_id: str) -> OutboxRecord | None:
        async with self._uow_factory() as uow:
            current = await uow.outbox.get_by_event_id(event_id)
        if current is None or current.published_at is not None:
            return None
        return current

    async def _release_claim(self, event_id: str, lease_token: str | None) -> None:
        if lease_token is None:  # pragma: no cover - claim 一定带 token
            return
        async with self._uow_factory() as uow:
            changed = await uow.outbox.release_claim(event_id, lease_token=lease_token)
            if changed:
                await uow.commit()

    async def _record_failure(self, event_id: str, exc: Exception, lease_token: str | None) -> None:
        async with self._uow_factory() as uow:
            current = await uow.outbox.get_by_event_id(event_id)
            if current is None or current.published_at is not None:
                await uow.outbox.release_claim(event_id, lease_token=lease_token or "")
                await uow.commit()
                return
            changed = await uow.outbox.record_failure(
                event_id, error=_safe_error(exc), lease_token=lease_token
            )
            if changed:
                await uow.commit()
            elif lease_token is not None:
                # 租约已不在我们手里(别的 worker 已处理),不要让记录停在占用态。
                await uow.outbox.release_claim(event_id, lease_token=lease_token)
                await uow.commit()

    async def _mark_published(self, event_id: str, lease_token: str | None) -> None:
        async with self._uow_factory() as uow:
            current = await uow.outbox.get_by_event_id(event_id)
            if current is None or current.published_at is not None:
                if lease_token is not None:
                    await uow.outbox.release_claim(event_id, lease_token=lease_token)
                    await uow.commit()
                return
            changed = await uow.outbox.mark_published(
                event_id, published_at=self._clock(), lease_token=lease_token
            )
            if changed:
                await uow.commit()
            elif lease_token is not None:
                await uow.outbox.release_claim(event_id, lease_token=lease_token)
                await uow.commit()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_error(exc: Exception) -> str:
    """Persist only an exception class so transport credentials cannot leak."""
    return type(exc).__name__
