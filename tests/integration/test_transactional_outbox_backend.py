"""Transactional outbox contract shared by all supported database backends."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from xianyu_agent.application.ports.outbox import OutboxEvent
from xianyu_agent.config import get_settings
from xianyu_agent.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


def _event(event_id: str, account_id: str) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        event_type="AccountConfigured",
        version=1,
        account_id=account_id,
        aggregate_type="account",
        aggregate_id=account_id,
        correlation_id=f"corr-{event_id}",
        causation_id=None,
        payload={"account_id": account_id, "enabled": True},
        occurred_at=datetime.now(UTC),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_business_mutation_and_outbox_share_one_transaction() -> None:
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    assert get_settings().database_backend == expected_backend
    account_id = f"outbox-account-{expected_backend}-{uuid4().hex[:8]}"
    rollback_event_id = f"rollback-{uuid4().hex}"
    committed_event_id = f"committed-{uuid4().hex}"

    # No commit: both business mutation and outbox insert must disappear together.
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.accounts.add(account_id, nickname="outbox")
        await uow.outbox.enqueue(_event(rollback_event_id, account_id))

    async with SqlAlchemyUnitOfWork() as uow:
        assert await uow.accounts.get_by_account_id(account_id) is None
        assert await uow.outbox.get_by_event_id(rollback_event_id) is None

    # Commit once: both rows become durable in the same database transaction.
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.accounts.add(account_id, nickname="outbox")
        await uow.outbox.enqueue(_event(committed_event_id, account_id))
        await uow.commit()

    # Simulate process death immediately after commit: a fresh UoW must recover
    # the unpublished event instead of losing it in the commit/publish gap.
    async with SqlAlchemyUnitOfWork() as uow:
        account = await uow.accounts.get_by_account_id(account_id)
        stored = await uow.outbox.get_by_event_id(committed_event_id)
        pending_ids = {row.event_id for row in await uow.outbox.list_pending(limit=1000)}

    assert account is not None
    assert stored is not None
    assert stored.published_at is None
    assert stored.attempt_count == 0
    assert committed_event_id in pending_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_attempt_and_publish_state_are_durable() -> None:
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    event_id = f"attempt-{expected_backend}-{uuid4().hex}"
    account_id = f"attempt-account-{uuid4().hex[:8]}"

    async with SqlAlchemyUnitOfWork() as uow:
        await uow.outbox.enqueue(_event(event_id, account_id))
        await uow.commit()

    async with SqlAlchemyUnitOfWork() as uow:
        assert await uow.outbox.record_failure(event_id, error="temporary transport error")
        await uow.commit()

    async with SqlAlchemyUnitOfWork() as uow:
        failed = await uow.outbox.get_by_event_id(event_id)
    assert failed is not None
    assert failed.attempt_count == 1
    assert failed.last_error == "temporary transport error"

    published_at = datetime.now(UTC)
    async with SqlAlchemyUnitOfWork() as uow:
        assert await uow.outbox.mark_published(event_id, published_at=published_at)
        await uow.commit()

    async with SqlAlchemyUnitOfWork() as uow:
        published = await uow.outbox.get_by_event_id(event_id)
        pending_ids = {row.event_id for row in await uow.outbox.list_pending(limit=1000)}
    assert published is not None
    assert published.published_at is not None
    assert published.last_error is None
    assert event_id not in pending_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbox_event_id_is_unique() -> None:
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    event_id = f"unique-{expected_backend}-{uuid4().hex}"
    account_id = f"unique-account-{uuid4().hex[:8]}"

    async with SqlAlchemyUnitOfWork() as uow:
        await uow.outbox.enqueue(_event(event_id, account_id))
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with SqlAlchemyUnitOfWork() as uow:
            await uow.outbox.enqueue(_event(event_id, account_id))
