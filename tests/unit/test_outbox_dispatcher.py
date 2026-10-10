"""Unit tests for at-least-once transactional-outbox dispatch."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import uuid4

import pytest

from xianyu_agent.application.outbox_dispatcher import DispatchResult, OutboxDispatcher
from xianyu_agent.application.ports.outbox import OutboxRecord

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LEASE = timedelta(seconds=30)


@dataclass
class _State:
    records: dict[str, OutboxRecord]
    leases: dict[str, tuple[str, datetime]] | None = None
    fail_next_commit: bool = False

    def lease_table(self) -> dict[str, tuple[str, datetime]]:
        return {} if self.leases is None else dict(self.leases)


class _OutboxRepository:
    """In-memory stand-in for the SQLAlchemy outbox repository (CAS 抢占语义一致)。"""

    def __init__(
        self,
        records: dict[str, OutboxRecord],
        leases: dict[str, tuple[str, datetime]],
    ) -> None:
        self._records = records
        self._leases = leases

    async def list_pending(self, *, limit: int = 100) -> list[OutboxRecord]:
        pending = [row for row in self._records.values() if row.published_at is None]
        return sorted(
            pending,
            key=lambda row: (row.attempt_count, row.occurred_at, row.id),
        )[:limit]

    async def claim_pending(
        self,
        *,
        owner: str,
        lease_duration: timedelta,
        limit: int = 100,
        now: datetime,
    ) -> list[OutboxRecord]:
        del owner  # 内存实现只需要 token 与到期时间
        claimed: list[OutboxRecord] = []
        for row in await self.list_pending(limit=limit):
            lease = self._leases.get(row.event_id)
            if lease is not None and lease[1] > now:
                continue
            token = uuid4().hex
            self._leases[row.event_id] = (token, now + lease_duration)
            claimed.append(replace(row, lease_token=token))
            if len(claimed) >= limit:
                break
        return claimed

    async def release_claim(self, event_id: str, *, lease_token: str) -> bool:
        lease = self._leases.get(event_id)
        if lease is None or lease[0] != lease_token:
            return False
        self._leases.pop(event_id, None)
        return True

    async def get_by_event_id(self, event_id: str) -> OutboxRecord | None:
        return self._records.get(event_id)

    async def mark_published(
        self, event_id: str, *, published_at: datetime, lease_token: str | None = None
    ) -> bool:
        row = self._records.get(event_id)
        if row is None or row.published_at is not None:
            return False
        if lease_token is not None and not await self.release_claim(
            event_id, lease_token=lease_token
        ):
            return False
        self._records[event_id] = replace(row, published_at=published_at, last_error=None)
        return True

    async def record_failure(
        self, event_id: str, *, error: str, lease_token: str | None = None
    ) -> bool:
        row = self._records.get(event_id)
        if row is None or row.published_at is not None:
            return False
        if lease_token is not None and not await self.release_claim(
            event_id, lease_token=lease_token
        ):
            return False
        self._records[event_id] = replace(
            row,
            attempt_count=row.attempt_count + 1,
            last_error=error,
        )
        return True


class _UnitOfWork:
    def __init__(self, state: _State) -> None:
        self._state = state
        self._working: dict[str, OutboxRecord] = {}
        self._leases: dict[str, tuple[str, datetime]] = {}
        self.outbox = _OutboxRepository(self._working, self._leases)

    async def __aenter__(self) -> Self:
        self._working = dict(self._state.records)
        self._leases = self._state.lease_table()
        self.outbox = _OutboxRepository(self._working, self._leases)
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        if self._state.fail_next_commit:
            self._state.fail_next_commit = False
            raise RuntimeError("simulated commit failure")
        self._state.records = dict(self._working)
        self._state.leases = dict(self._leases)

    async def rollback(self) -> None:
        return None


class _RecordingBus:
    def __init__(
        self,
        *,
        failures: int = 0,
        failure_message: str = "temporary bus failure",
        state: _State | None = None,
        fail_commit_after_first_publish: bool = False,
    ) -> None:
        self._failures = failures
        self._failure_message = failure_message
        self._state = state
        self._fail_commit_after_first_publish = fail_commit_after_first_publish
        self.published: list[str] = []

    async def publish(self, event: OutboxRecord) -> None:
        if self._failures:
            self._failures -= 1
            raise RuntimeError(self._failure_message)
        self.published.append(event.event_id)
        if self._fail_commit_after_first_publish and self._state is not None:
            self._fail_commit_after_first_publish = False
            self._state.fail_next_commit = True


def _record(event_id: str = "event-1") -> OutboxRecord:
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    return OutboxRecord(
        id=1,
        event_id=event_id,
        event_type="AccountConfigured",
        version=1,
        account_id="account-a",
        aggregate_type="account",
        aggregate_id="account-a",
        correlation_id="corr-1",
        causation_id=None,
        payload={"enabled": True},
        occurred_at=occurred_at,
        published_at=None,
        attempt_count=0,
        last_error=None,
    )


def _dispatcher(
    state: _State,
    bus: _RecordingBus,
    published_at: datetime,
    *,
    clock: object | None = None,
) -> OutboxDispatcher:
    return OutboxDispatcher(
        lambda: _UnitOfWork(state),
        bus,
        clock=clock or (lambda: published_at),  # type: ignore[arg-type]
        owner="dispatcher-a",
        lease_duration=LEASE,
    )


@pytest.mark.asyncio
async def test_dispatch_once_marks_published_only_after_bus_accepts_event() -> None:
    row = _record()
    state = _State(records={row.event_id: row})
    bus = _RecordingBus()
    published_at = datetime(2026, 1, 2, tzinfo=UTC)

    result = await _dispatcher(state, bus, published_at).dispatch_once()

    assert result == DispatchResult(selected=1, published=1, failed=0, skipped=0)
    assert bus.published == [row.event_id]
    stored = state.records[row.event_id]
    assert stored.published_at == published_at
    assert stored.attempt_count == 0
    assert stored.last_error is None
    assert state.lease_table() == {}


@pytest.mark.asyncio
async def test_publish_failure_stays_pending_without_persisting_exception_detail() -> None:
    row = _record()
    state = _State(records={row.event_id: row})
    bus = _RecordingBus(
        failures=1,
        failure_message="Authorization: Bearer super-secret-token",
    )
    dispatcher = _dispatcher(state, bus, datetime(2026, 1, 2, tzinfo=UTC))

    first = await dispatcher.dispatch_once()
    failed = state.records[row.event_id]

    assert first == DispatchResult(selected=1, published=0, failed=1, skipped=0)
    assert failed.published_at is None
    assert failed.attempt_count == 1
    assert failed.last_error == "RuntimeError"
    # 失败即释放租约,下一次抢占可以立刻重试。
    assert state.lease_table() == {}

    second = await dispatcher.dispatch_once()

    assert second == DispatchResult(selected=1, published=1, failed=0, skipped=0)
    assert bus.published == [row.event_id]
    published = state.records[row.event_id]
    assert published.published_at is not None
    assert published.attempt_count == 1
    assert published.last_error is None


@pytest.mark.asyncio
async def test_publish_then_mark_commit_failure_redelivers_after_lease_expiry() -> None:
    """发布成功但标记提交失败时,事件仍会重投,只是要等租约到期(M1 语义)。"""
    row = _record()
    state = _State(records={row.event_id: row})
    bus = _RecordingBus(state=state, fail_commit_after_first_publish=True)
    now = [T0]
    dispatcher = OutboxDispatcher(
        lambda: _UnitOfWork(state),
        bus,
        clock=lambda: now[0],
        owner="dispatcher-a",
        lease_duration=LEASE,
    )

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await dispatcher.dispatch_once()

    assert bus.published == [row.event_id]
    assert state.records[row.event_id].published_at is None

    # 租约未过期:任何副本都不能重复发布。
    blocked = await dispatcher.dispatch_once()
    assert blocked == DispatchResult(selected=0, published=0, failed=0, skipped=0)

    now[0] = T0 + LEASE + timedelta(seconds=1)
    result = await dispatcher.dispatch_once()

    assert result == DispatchResult(selected=1, published=1, failed=0, skipped=0)
    assert bus.published == [row.event_id, row.event_id]
    assert state.records[row.event_id].published_at is not None


@pytest.mark.asyncio
async def test_claim_is_exclusive_until_the_lease_expires() -> None:
    """两个副本抢同一批事件时,未过期的租约阻止第二个副本拿到同一条。"""
    row = _record()
    state = _State(records={row.event_id: row})

    first_uow = _UnitOfWork(state)
    await first_uow.__aenter__()
    claimed = await first_uow.outbox.claim_pending(
        owner="a", lease_duration=LEASE, limit=10, now=T0
    )
    await first_uow.commit()

    assert [item.event_id for item in claimed] == [row.event_id]
    assert claimed[0].lease_token

    second_uow = _UnitOfWork(state)
    await second_uow.__aenter__()
    blocked = await second_uow.outbox.claim_pending(
        owner="b", lease_duration=LEASE, limit=10, now=T0 + timedelta(seconds=1)
    )
    assert blocked == []

    later_uow = _UnitOfWork(state)
    await later_uow.__aenter__()
    takeover = await later_uow.outbox.claim_pending(
        owner="b", lease_duration=LEASE, limit=10, now=T0 + LEASE + timedelta(seconds=1)
    )

    assert [item.event_id for item in takeover] == [row.event_id]
    assert takeover[0].lease_token != claimed[0].lease_token
