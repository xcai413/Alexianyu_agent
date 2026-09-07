"""Lifecycle contracts for the Scheduler Kernel wake-up loop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from xianyu_agent.runtime.scheduler import RunResult, SchedulerLoop


@dataclass(slots=True)
class FakeRecovery:
    calls: int = 0

    async def recover_once(self) -> int:
        self.calls += 1
        return 0


@dataclass(slots=True)
class FakeRunner:
    results: Iterable[RunResult]
    calls: int = 0
    call_events: list[asyncio.Event] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._results = iter(self.results)

    async def run_once(self) -> RunResult:
        self.calls += 1
        while len(self.call_events) < self.calls:
            self.call_events.append(asyncio.Event())
        self.call_events[self.calls - 1].set()
        return next(
            self._results,
            RunResult(claimed=0, completed=0, released=0, stale=0),
        )


async def _wait_for_call(runner: FakeRunner, number: int) -> None:
    while len(runner.call_events) < number:
        await asyncio.sleep(0)
    await runner.call_events[number - 1].wait()


@pytest.mark.asyncio
async def test_loop_recovers_once_and_stops_from_idle_wait() -> None:
    runner = FakeRunner([RunResult(claimed=0, completed=0, released=0, stale=0)])
    recovery = FakeRecovery()
    loop = SchedulerLoop(runner, recovery, idle_interval=timedelta(hours=1))  # type: ignore[arg-type]

    task = asyncio.create_task(loop.run())
    await _wait_for_call(runner, 1)
    loop.stop()
    await task

    assert recovery.calls == 1
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_wake_interrupts_idle_wait_for_immediate_pass() -> None:
    runner = FakeRunner(
        [
            RunResult(claimed=0, completed=0, released=0, stale=0),
            RunResult(claimed=0, completed=0, released=0, stale=0),
        ]
    )
    loop = SchedulerLoop(
        runner, FakeRecovery(), idle_interval=timedelta(hours=1)  # type: ignore[arg-type]
    )

    task = asyncio.create_task(loop.run())
    await _wait_for_call(runner, 1)
    loop.wake()
    await _wait_for_call(runner, 2)
    loop.stop()
    await task

    assert runner.calls == 2


@pytest.mark.asyncio
async def test_claimed_work_drains_without_waiting_for_wake_or_poll() -> None:
    runner = FakeRunner(
        [
            RunResult(claimed=1, completed=1, released=0, stale=0),
            RunResult(claimed=0, completed=0, released=0, stale=0),
        ]
    )
    loop = SchedulerLoop(
        runner, FakeRecovery(), idle_interval=timedelta(hours=1)  # type: ignore[arg-type]
    )

    task = asyncio.create_task(loop.run())
    await _wait_for_call(runner, 2)
    loop.stop()
    await task

    assert runner.calls == 2


def test_loop_rejects_non_positive_idle_interval() -> None:
    runner = FakeRunner([])
    recovery = FakeRecovery()

    with pytest.raises(ValueError, match="idle_interval must be positive"):
        SchedulerLoop(runner, recovery, idle_interval=timedelta(0))  # type: ignore[arg-type]
