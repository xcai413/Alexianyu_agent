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
async def test_transactional_outbox_contract_across_backend() -> None:
    """Exercise the full backend contract on one asyncio loop.

    The project owns one process-level async engine/session factory. Keeping this
    backend contract in one async test avoids reusing pooled asyncpg connections
    across pytest-created event loops while still opening fresh UoWs to model
    transaction/process boundaries.
    """
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    assert get_settings().database_backend == expected_backend

    # 1) No commit: business mutation and outbox insert disappear together.
    business_account_id = f"outbox-account-{expected_backend}-{uuid4().hex[:8]}"
    rollback_event_id = f"rollback-{uuid4().hex}"
    committed_event_id = f"committed-{uuid4().hex}"

    async with SqlAlchemyUnitOfWork() as uow:
        await uow.accounts.add(business_account_id, nickname="outbox")
        await uow.outbox.enqueue(_event(rollback_event_id, business_account_id))

    async with SqlAlchemyUnitOfWork() as uow:
        assert await uow.accounts.get_by_account_id(business_account_id) is None
        assert await uow.outbox.get_by_event_id(rollback_event_id) is None

    # 2) One commit makes both rows durable in the same database transaction.
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.accounts.add(business_account_id, nickname="outbox")
        await uow.outbox.enqueue(_event(committed_event_id, business_account_id))
        await uow.commit()

    # 3) Simulate commit-then-process-death gap: a fresh UoW must recover the
    # unpublished event even though no publish step ran after the commit.
    async with SqlAlchemyUnitOfWork() as uow:
        account = await uow.accounts.get_by_account_id(business_account_id)
        stored = await uow.outbox.get_by_event_id(committed_event_id)
        pending_ids = {row.event_id for row in await uow.outbox.list_pending(limit=1000)}

    assert account is not None
    assert stored is not None
    assert stored.published_at is None
    assert stored.attempt_count == 0
    assert committed_event_id in pending_ids

    # 4) Failure attempt metadata remains durable across fresh UoWs.
    attempt_event_id = f"attempt-{expected_backend}-{uuid4().hex}"
    attempt_account_id = f"attempt-account-{uuid4().hex[:8]}"

    async with SqlAlchemyUnitOfWork() as uow:
        await uow.outbox.enqueue(_event(attempt_event_id, attempt_account_id))
        await uow.commit()

    async with SqlAlchemyUnitOfWork() as uow:
        assert await uow.outbox.record_failure(
            attempt_event_id,
            error="temporary transport error",
        )
        await uow.commit()

    async with SqlAlchemyUnitOfWork() as uow:
        failed = await uow.outbox.get_by_event_id(attempt_event_id)

    assert failed is not None
    assert failed.attempt_count == 1
    assert failed.last_error == "temporary transport error"

    # 5) Published events are durable and disappear from the pending query.
    published_at = datetime.now(UTC)
    async with SqlAlchemyUnitOfWork() as uow:
        assert await uow.outbox.mark_published(attempt_event_id, published_at=published_at)
        await uow.commit()

    async with SqlAlchemyUnitOfWork() as uow:
        published = await uow.outbox.get_by_event_id(attempt_event_id)
        pending_ids = {row.event_id for row in await uow.outbox.list_pending(limit=1000)}

    assert published is not None
    assert published.published_at is not None
    assert published.last_error is None
    assert attempt_event_id not in pending_ids

    # 6) event_id uniqueness is enforced by the database, not just Python code.
    unique_event_id = f"unique-{expected_backend}-{uuid4().hex}"
    unique_account_id = f"unique-account-{uuid4().hex[:8]}"

    async with SqlAlchemyUnitOfWork() as uow:
        await uow.outbox.enqueue(_event(unique_event_id, unique_account_id))
        await uow.commit()

    with pytest.raises(IntegrityError):
        async with SqlAlchemyUnitOfWork() as uow:
            await uow.outbox.enqueue(_event(unique_event_id, unique_account_id))
