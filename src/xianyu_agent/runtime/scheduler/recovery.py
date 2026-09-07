"""Restart recovery for scheduler leases."""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock, require_utc
from .due_queue import DueQueue


@dataclass(slots=True)
class SchedulerRecovery:
    """Return expired durable leases to the due queue after restart."""

    queue: DueQueue
    clock: Clock

    async def recover_once(self) -> int:
        now = require_utc(self.clock.now())
        return await self.queue.recover_expired(now=now)
