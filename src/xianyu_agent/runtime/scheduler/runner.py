"""Bounded, persistence-neutral scheduler runner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from .clock import Clock, require_utc
from .due_queue import DueQueue
from .lease import LeasedWork

WorkHandler = Callable[[LeasedWork], Awaitable[None]]


class WorkOutcome(StrEnum):
    COMPLETED = "completed"
    RELEASED = "released"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Summary of one bounded scheduler pass."""

    claimed: int
    completed: int
    released: int
    stale: int


class SchedulerRunner:
    """Claim and execute due work without embedding business scheduling rules."""

    def __init__(
        self,
        queue: DueQueue,
        handler: WorkHandler,
        clock: Clock,
        *,
        lease_owner: str,
        lease_duration: timedelta = timedelta(minutes=1),
        retry_delay: timedelta = timedelta(seconds=5),
        batch_size: int = 100,
        concurrency: int = 4,
    ) -> None:
        if not lease_owner:
            raise ValueError("lease_owner must not be empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay must not be negative")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if concurrency <= 0:
            raise ValueError("concurrency must be positive")
        self._queue = queue
        self._handler = handler
        self._clock = clock
        self._lease_owner = lease_owner
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._batch_size = batch_size
        self._concurrency = concurrency

    async def run_once(self) -> RunResult:
        """Claim at most ``batch_size`` due items without parking active leases."""
        outcomes: list[WorkOutcome] = []
        claimed_count = 0

        while claimed_count < self._batch_size:
            remaining = self._batch_size - claimed_count
            claim_limit = min(self._concurrency, remaining)
            now = require_utc(self._clock.now())
            claimed = await self._queue.claim_due(
                now=now,
                lease_owner=self._lease_owner,
                lease_duration=self._lease_duration,
                limit=claim_limit,
            )
            if not claimed:
                break

            claimed_count += len(claimed)
            outcomes.extend(await asyncio.gather(*(self._execute(work) for work in claimed)))

            if len(claimed) < claim_limit:
                break

        return RunResult(
            claimed=claimed_count,
            completed=outcomes.count(WorkOutcome.COMPLETED),
            released=outcomes.count(WorkOutcome.RELEASED),
            stale=outcomes.count(WorkOutcome.STALE),
        )

    async def _execute(self, work: LeasedWork) -> WorkOutcome:
        try:
            await self._handler(work)
        except Exception:
            available_at = require_utc(self._clock.now()) + self._retry_delay
            released = await self._queue.release(work, available_at=available_at)
            return WorkOutcome.RELEASED if released else WorkOutcome.STALE
        completed_at = require_utc(self._clock.now())
        completed = await self._queue.complete(work, completed_at=completed_at)
        return WorkOutcome.COMPLETED if completed else WorkOutcome.STALE
