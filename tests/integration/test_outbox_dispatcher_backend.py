"""Outbox dispatcher contract shared by all supported database backends."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from xianyu_agent.application.outbox_dispatcher import DispatchResult, OutboxDispatcher
from xianyu_agent.application.ports.outbox import OutboxEvent, OutboxRecord
from xianyu_agent.config import get_settings
from xianyu_agent.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


class _RecordingBus:
    def __init__(self, *, failures: int = 0) -> None:
        self._failures = failures
        self.published: list[str] = []

    async def publish(self, event: OutboxRecord) -> None:
        if self._failures:
            self._failures -= 1
            raise RuntimeError("temporary bus failure")
        self.published.append(event.event_id)


def _event(event_id: str) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        event_type="DispatcherContractEvent",
        version=1,
        correlation_id=f"corr-{uuid4().hex}",
        payload={"event_id": event_id},
        occurred_at=datetime.now(UTC),
    )


def _event_id(kind: str, backend: str) -> str:
    """Build an identifier within the persisted 64-character event_id contract."""
    return f"disp-{kind}-{backend}-{uuid4().hex[:12]}"


async def _enqueue(event_id: str) -> None:
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.outbox.enqueue(_event(event_id))
        await uow.commit()


async def _get(event_id: str) -> OutboxRecord:
    async with SqlAlchemyUnitOfWork() as uow:
        row = await uow.outbox.get_by_event_id(event_id)
    assert row is not None
    return row


async def _assert_success_case(backend: str) -> None:
    event_id = _event_id("success", backend)
    await _enqueue(event_id)
    bus = _RecordingBus()

    result = await OutboxDispatcher(SqlAlchemyUnitOfWork, bus).dispatch_once()

    assert result.published >= 1
    assert result.failed == 0
    assert event_id in bus.published
    stored = await _get(event_id)
    assert stored.published_at is not None
    assert stored.attempt_count == 0
    assert stored.last_error is None


async def _assert_retry_case(backend: str) -> None:
    event_id = _event_id("retry", backend)
    await _enqueue(event_id)
    bus = _RecordingBus(failures=1)
    dispatcher = OutboxDispatcher(SqlAlchemyUnitOfWork, bus, batch_size=1000)

    first = await dispatcher.dispatch_once()
    failed = await _get(event_id)

    assert first == DispatchResult(selected=1, published=0, failed=1, skipped=0)
    assert failed.published_at is None
    assert failed.attempt_count == 1
    assert failed.last_error == "RuntimeError: temporary bus failure"

    second = await dispatcher.dispatch_once()
    published = await _get(event_id)

    assert second == DispatchResult(selected=1, published=1, failed=0, skipped=0)
    assert bus.published == [event_id]
    assert published.published_at is not None
    assert published.attempt_count == 1
    assert published.last_error is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_dispatcher_contract_across_backend() -> None:
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    assert get_settings().database_backend == expected_backend
    await _assert_success_case(expected_backend)
    await _assert_retry_case(expected_backend)
