"""Cancellable wake-up loop for the business-neutral Scheduler Kernel."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from .recovery import SchedulerRecovery
from .runner import SchedulerRunner


class SchedulerLoop:
    """Drive recovery and bounded scheduler passes without business semantics.

    The loop drains immediately while a pass claims work, otherwise it sleeps for
    a bounded polling interval. Producers may call :meth:`wake` after persisting
    newly due work so the loop does not need to wait for the next poll. ``stop``
    wakes an idle loop and causes deterministic shutdown after the current pass.
    """

    def __init__(
        self,
        runner: SchedulerRunner,
        recovery: SchedulerRecovery,
        *,
        idle_interval: timedelta = timedelta(seconds=1),
    ) -> None:
        if idle_interval <= timedelta(0):
            raise ValueError("idle_interval must be positive")
        self._runner = runner
        self._recovery = recovery
        self._idle_seconds = idle_interval.total_seconds()
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()

    def wake(self) -> None:
        """Request an immediate scheduler pass after newly due work is persisted."""
        self._wake_event.set()

    def stop(self) -> None:
        """Request shutdown and interrupt an idle wait."""
        self._stop_event.set()
        self._wake_event.set()

    async def run(self) -> None:
        """Recover expired leases once, then process work until stopped."""
        await self._recovery.recover_once()

        while not self._stop_event.is_set():
            # Clear before the pass so a wake arriving during run_once remains
            # observable by the following idle wait instead of being lost.
            self._wake_event.clear()
            result = await self._runner.run_once()

            if self._stop_event.is_set():
                break
            if result.claimed > 0:
                continue

            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=self._idle_seconds)
            except TimeoutError:
                pass
