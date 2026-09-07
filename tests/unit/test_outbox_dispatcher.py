"""Unit tests for at-least-once transactional-outbox dispatch."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest

from xianyu_agent.application.outbox_dispatcher import DispatchResult, OutboxDispatcher
from xianyu_agent.application.ports.outbox import OutboxRecord


@dataclass
class _State:
    records: dict[str, OutboxRecord]
    fail_next_commit: bool = False


class _OutboxRepository:
    def __init__(self, records: dict[str, OutboxRecord]) -> None:
        self._records = records

    async def list_pending(self, *, limit: int = 100) -> list[OutboxRecord]:
        pending = [row for row in self._records.values() if row.published_at is None]
        return sorted(
            pending,
            key=lambda row: (row.attempt_count, row.occurred_at, row.id),
        )[:limit]

    async def get_by_event_id(self, event_id: str) -> OutboxRecord | None:
        return self._records.get(event_id)

    async def mark_published(self, event_id: str, *, published_at: datetime) -> bool:
        row = self._records.get(event_id)
        if row is None:
            return False
        self._records[event_id] = replace(row, published_at=published_at, last_error=None)
        return True

    async def record_failure(self, event_id: str, *, error: str) -> bool:
        row = self._records.get(event_id)
        if row is None:
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
        self.outbox = _OutboxRepository(self._working)

    async def __aenter__(self) -> Self:
        self._working = dict(self._state.records)
        self.outbox = _OutboxRepository(self._working)
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


def _dispatcher(state: _State, bus: _RecordingBus, published_at: datetime) -> OutboxDispatcher:
    return OutboxDispatcher(
        lambda: _UnitOfWork(state),
        bus,
        clock=lambda: published_at,
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

    second = await dispatcher.dispatch_once()

    assert second == DispatchResult(selected=1, published=1, failed=0, skipped=0)
    assert bus.published == [row.event_id]
    published = state.records[row.event_id]
    assert published.published_at is not None
    assert published.attempt_count == 1
    assert published.last_error is None


@pytest.mark.asyncio
async def test_publish_then_mark_commit_failure_causes_at_least_once_redelivery() -> None:
    row = _record()
    state = _State(records={row.event_id: row})
    bus = _RecordingBus(state=state, fail_commit_after_first_publish=True)
    dispatcher = _dispatcher(state, bus, datetime(2026, 1, 2, tzinfo=UTC))

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await dispatcher.dispatch_once()

    assert bus.published == [row.event_id]
    assert state.records[row.event_id].published_at is None

    result = await dispatcher.dispatch_once()

    assert result == DispatchResult(selected=1, published=1, failed=0, skipped=0)
    assert bus.published == [row.event_id, row.event_id]
    assert state.records[row.event_id].published_at is not None
