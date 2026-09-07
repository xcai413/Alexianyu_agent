"""Contracts for the persistence-neutral scheduler kernel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from xianyu_agent.runtime.scheduler import LeasedWork, SchedulerRecovery, SchedulerRunner


@dataclass(slots=True)
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


@dataclass(slots=True)
class FakeQueue:
    claimed: tuple[LeasedWork, ...] = ()
    claim_limits: list[int] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    released: list[tuple[str, datetime]] = field(default_factory=list)
    recovered: int = 0

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[LeasedWork, ...]:
        self.claim_limits.append(limit)
        batch = self.claimed[:limit]
        self.claimed = self.claimed[len(batch) :]
        return batch

    async def complete(self, work: LeasedWork, *, completed_at: datetime) -> bool:
        self.completed.append(work.work_id)
        return True

    async def release(self, work: LeasedWork, *, available_at: datetime) -> bool:
        self.released.append((work.work_id, available_at))
        return True

    async def recover_expired(self, *, now: datetime) -> int:
        return self.recovered


def _lease(work_id: str) -> LeasedWork:
    return LeasedWork(
        work_id=work_id,
        lease_token=f"token-{work_id}",
        lease_owner="worker-1",
        leased_until=datetime(2026, 9, 8, 1, 1, tzinfo=UTC),
        payload={"opaque": work_id},
    )


@pytest.mark.asyncio
async def test_runner_bounds_claims_and_completes_successful_work() -> None:
    queue = FakeQueue(claimed=(_lease("one"), _lease("two"), _lease("three")))
    clock = FakeClock(datetime(2026, 9, 8, 1, 0, tzinfo=UTC))
    handled: list[str] = []

    async def handler(work: LeasedWork) -> None:
        handled.append(work.work_id)

    result = await SchedulerRunner(
        queue,
        handler,
        clock,
        lease_owner="worker-1",
        batch_size=2,
        concurrency=2,
    ).run_once()

    assert queue.claim_limits == [2]
    assert handled == ["one", "two"]
    assert queue.completed == ["one", "two"]
    assert result.claimed == 2
    assert result.completed == 2
    assert result.released == 0
    assert result.stale == 0


@pytest.mark.asyncio
async def test_runner_claims_only_immediately_executable_capacity() -> None:
    queue = FakeQueue(claimed=tuple(_lease(str(index)) for index in range(5)))
    clock = FakeClock(datetime(2026, 9, 8, 1, 0, tzinfo=UTC))
    gates = [asyncio.Event() for _ in range(5)]
    started_events = [asyncio.Event() for _ in range(5)]
    started: list[str] = []

    async def handler(work: LeasedWork) -> None:
        index = int(work.work_id)
        started.append(work.work_id)
        started_events[index].set()
        await gates[index].wait()

    task = asyncio.create_task(
        SchedulerRunner(
            queue,
            handler,
            clock,
            lease_owner="worker-1",
            batch_size=5,
            concurrency=2,
        ).run_once()
    )
    await asyncio.gather(started_events[0].wait(), started_events[1].wait())

    assert queue.claim_limits == [2]
    assert started == ["0", "1"]

    gates[0].set()
    gates[1].set()
    await asyncio.gather(started_events[2].wait(), started_events[3].wait())

    assert queue.claim_limits == [2, 2]
    assert started == ["0", "1", "2", "3"]

    gates[2].set()
    gates[3].set()
    await started_events[4].wait()

    assert queue.claim_limits == [2, 2, 1]
    assert started == ["0", "1", "2", "3", "4"]

    gates[4].set()
    result = await task
    assert result.claimed == 5
    assert result.completed == 5


@pytest.mark.asyncio
async def test_runner_releases_failed_work_with_retry_delay() -> None:
    queue = FakeQueue(claimed=(_lease("bad"),))
    now = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    clock = FakeClock(now)

    async def handler(work: LeasedWork) -> None:
        raise RuntimeError("credential=must-not-be-persisted")

    result = await SchedulerRunner(
        queue,
        handler,
        clock,
        lease_owner="worker-1",
        retry_delay=timedelta(seconds=7),
    ).run_once()

    assert queue.released == [("bad", now + timedelta(seconds=7))]
    assert result.completed == 0
    assert result.released == 1
    assert result.stale == 0


@pytest.mark.asyncio
async def test_runner_enforces_concurrency_limit() -> None:
    queue = FakeQueue(claimed=tuple(_lease(str(index)) for index in range(4)))
    clock = FakeClock(datetime(2026, 9, 8, 1, 0, tzinfo=UTC))
    active = 0
    peak = 0

    async def handler(work: LeasedWork) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1

    await SchedulerRunner(
        queue,
        handler,
        clock,
        lease_owner="worker-1",
        concurrency=2,
        batch_size=4,
    ).run_once()

    assert queue.claim_limits == [2, 2]
    assert peak == 2


@pytest.mark.asyncio
async def test_recovery_delegates_expired_lease_recovery_at_utc_now() -> None:
    queue = FakeQueue(recovered=3)
    clock = FakeClock(datetime(2026, 9, 8, 1, 0, tzinfo=UTC))

    assert await SchedulerRecovery(queue, clock).recover_once() == 3


def test_leased_work_rejects_naive_timestamps_and_freezes_payload() -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        LeasedWork(
            work_id="one",
            lease_token="token",
            lease_owner="worker",
            leased_until=datetime(2026, 9, 8, 1, 0),
        )

    lease = _lease("one")
    with pytest.raises(TypeError):
        lease.payload["mutate"] = True  # type: ignore[index]
