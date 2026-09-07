"""Account application/UoW contract shared by all supported database backends."""

from __future__ import annotations

import os

import pytest

from xianyu_agent.application.accounts import AccountApplication
from xianyu_agent.config import get_settings
from xianyu_agent.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


@pytest.mark.integration
@pytest.mark.asyncio
async def test_account_application_uow_is_consistent_across_backends() -> None:
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    settings = get_settings()
    assert settings.database_backend == expected_backend

    application = AccountApplication(SqlAlchemyUnitOfWork)
    account_id = f"application-uow-{expected_backend}"
    rollback_id = f"application-uow-rollback-{expected_backend}"

    await application.delete_account(account_id)
    await application.delete_account(rollback_id)

    try:
        # Leaving a concrete UoW without commit must roll back repository flushes.
        async with SqlAlchemyUnitOfWork() as uow:
            await uow.accounts.add(rollback_id, nickname="rollback")
        assert await application.get_account(rollback_id) is None

        created = await application.create_account(
            account_id,
            nickname="primary",
            remark="before",
        )
        duplicate = await application.create_account(
            account_id,
            nickname="must-not-overwrite",
            remark="must-not-overwrite",
        )
        assert duplicate.id == created.id
        assert duplicate.nickname == "primary"
        assert duplicate.remark == "before"

        assert await application.set_remark(account_id, "after")
        updated = await application.get_account(account_id)
        assert updated is not None
        assert updated.remark == "after"

        assert await application.set_desired_state(account_id, "running")
        running_ids = {
            row.account_id for row in await application.list_desired_running_accounts()
        }
        assert account_id in running_ids

        assert await application.set_enabled(account_id, False)
        disabled = await application.get_account(account_id)
        assert disabled is not None
        assert disabled.enabled is False
        assert disabled.desired_state == "stopped"

        assert not await application.set_enabled("application-uow-missing", False)
        assert not await application.delete_account("application-uow-missing")
    finally:
        await application.delete_account(account_id)
        await application.delete_account(rollback_id)
