"""Repository contract checks shared by SQLite, MySQL, and PostgreSQL CI jobs."""

from __future__ import annotations

import os

import pytest

from xianyu_agent.config import get_settings
from xianyu_agent.db.database import get_async_session
from xianyu_agent.db.models import WorkerDesiredState
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyAccountRepository


@pytest.mark.integration
@pytest.mark.asyncio
async def test_account_repository_contract_is_consistent_across_backends() -> None:
    """Exercise one persistence contract with identical semantics on all three DBs."""
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    settings = get_settings()
    assert settings.database_backend == expected_backend

    account_a = f"repo-contract-{expected_backend}-a"
    account_b = f"repo-contract-{expected_backend}-b"

    # Repository owns persistence operations, but transaction commit/rollback belongs
    # to the caller. A flushed row must therefore disappear after caller rollback.
    async with get_async_session() as session:
        repo = SqlAlchemyAccountRepository(session)
        await repo.delete(account_a)
        await repo.delete(account_b)
        await session.commit()

        created = await repo.add(account_a, nickname="rollback-probe")
        assert created.account_id == account_a
        await session.rollback()

    async with get_async_session() as session:
        repo = SqlAlchemyAccountRepository(session)
        assert await repo.get_by_account_id(account_a) is None

        created_a = await repo.add(account_a, nickname="A", remark="primary")
        created_b = await repo.add(account_b, nickname="B", enabled=False)
        duplicate_a = await repo.add(account_a, nickname="must-not-overwrite")
        assert duplicate_a.id == created_a.id
        assert duplicate_a.nickname == "A"
        assert created_b.enabled is False
        await session.commit()

    async with get_async_session() as session:
        repo = SqlAlchemyAccountRepository(session)

        enabled_ids = {row.account_id for row in await repo.list(only_enabled=True)}
        assert account_a in enabled_ids
        assert account_b not in enabled_ids

        assert await repo.set_desired_state(account_a, WorkerDesiredState.RUNNING.value)
        running_ids = {
            row.account_id
            for row in await repo.list(desired_state=WorkerDesiredState.RUNNING.value)
        }
        assert account_a in running_ids

        assert await repo.set_enabled(account_a, False)
        disabled_a = await repo.get_by_account_id(account_a)
        assert disabled_a is not None
        assert disabled_a.enabled is False
        assert disabled_a.desired_state == WorkerDesiredState.STOPPED.value

        with pytest.raises(ValueError, match="invalid desired_state"):
            await repo.set_desired_state(account_a, "invalid")

        assert await repo.delete(account_b)
        assert not await repo.delete("repository-contract-missing")
        await session.commit()

    async with get_async_session() as session:
        repo = SqlAlchemyAccountRepository(session)
        assert await repo.get_by_account_id(account_b) is None
        await repo.delete(account_a)
        await session.commit()
