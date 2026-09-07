"""Consumer inbox idempotency contract shared by all supported databases."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import database as legacy_database
from xianyu_agent.foundation.identifiers import IdempotencyKey
from xianyu_agent.infrastructure.database.models.idempotency import ConsumerInbox
from xianyu_agent.infrastructure.idempotency import SqlAlchemyIdempotencyStore


async def _claim_then_crash(consumer: str, key: IdempotencyKey) -> None:
    async with legacy_database.async_session_factory() as session, session.begin():
        store = SqlAlchemyIdempotencyStore(session)
        assert await store.claim(consumer, key) is True
        raise RuntimeError("crash before commit")


async def _assert_atomic_durable_and_consumer_scoped() -> None:
    suffix = uuid4().hex
    consumer = f"test-consumer-{suffix}"
    other_consumer = f"other-consumer-{suffix}"
    key = IdempotencyKey(f"event-{suffix}-" + "x" * 256)

    with pytest.raises(RuntimeError, match="crash before commit"):
        await _claim_then_crash(consumer, key)

    async with legacy_database.async_session_factory() as session, session.begin():
        store = SqlAlchemyIdempotencyStore(session)
        assert await store.claim(consumer, key) is True
        assert await store.claim(consumer, key) is False

    async with legacy_database.async_session_factory() as session, session.begin():
        store = SqlAlchemyIdempotencyStore(session)
        assert await store.claim(consumer, key) is False
        assert await store.claim(other_consumer, key) is True

    async with legacy_database.async_session_factory() as session:
        rows = (
            await session.scalars(
                select(ConsumerInbox).where(
                    ConsumerInbox.consumer.in_([consumer, other_consumer])
                )
            )
        ).all()
        assert {row.consumer for row in rows} == {consumer, other_consumer}
        assert all(len(row.key_hash) == 64 for row in rows)
        assert all(key.value not in row.key_hash for row in rows)


async def _assert_consumer_namespace_is_canonical() -> None:
    suffix = uuid4().hex
    key = IdempotencyKey(f"event-{suffix}")
    async with legacy_database.async_session_factory() as session, session.begin():
        store = SqlAlchemyIdempotencyStore(session)
        assert await store.claim(f" Email.Worker-{suffix} ", key) is True
        assert await store.claim(f"email.worker-{suffix}", key) is False


async def _assert_invalid_consumer_namespace_is_rejected() -> None:
    async with legacy_database.async_session_factory() as session, session.begin():
        store = SqlAlchemyIdempotencyStore(session)
        with pytest.raises(ValueError, match="must not be empty"):
            await store.claim("   ", IdempotencyKey("key"))
        with pytest.raises(ValueError, match="must not exceed 128"):
            await store.claim("c" * 129, IdempotencyKey("key"))
        with pytest.raises(ValueError, match="ASCII slug"):
            await store.claim("émail", IdempotencyKey("key"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotency_contract_across_backend() -> None:
    """Exercise every backend assertion on one asyncio event loop."""
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend

    await _assert_atomic_durable_and_consumer_scoped()
    await _assert_consumer_namespace_is_canonical()
    await _assert_invalid_consumer_namespace_is_rejected()
