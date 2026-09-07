"""Persistence-neutral due-work queue contract."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .lease import LeasedWork


class DueQueue(Protocol):
    """Durable queue operations required by the scheduler kernel.

    Implementations must claim work atomically so two workers cannot hold the
    same active lease. Expired leases become recoverable after ``leased_until``.
    """

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[LeasedWork, ...]: ...

    async def complete(self, work: LeasedWork, *, completed_at: datetime) -> bool: ...

    async def release(self, work: LeasedWork, *, available_at: datetime) -> bool: ...

    async def recover_expired(self, *, now: datetime) -> int: ...
