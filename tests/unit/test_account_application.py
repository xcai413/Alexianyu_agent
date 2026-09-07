"""Unit tests for the account application transaction boundary."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from xianyu_agent.application.accounts import AccountApplication
from xianyu_agent.application.ports.repositories import AccountRecord


def _record(*, desired_state: str = "stopped", remark: str | None = None) -> AccountRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return AccountRecord(
        id=1,
        account_id="account-a",
        nickname="A",
        remark=remark,
        enabled=True,
        desired_state=desired_state,
        status="offline",
        last_login_at=None,
        last_heartbeat_at=None,
        created_at=now,
        updated_at=now,
    )


def _uow(repo: MagicMock) -> MagicMock:
    uow = MagicMock()
    uow.accounts = repo
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=None)
    uow.commit = AsyncMock()
    uow.rollback = AsyncMock()
    return uow


@pytest.mark.asyncio
async def test_create_account_commits_once_and_returns_projection() -> None:
    repo = MagicMock()
    repo.add = AsyncMock(return_value=_record())
    uow = _uow(repo)
    application = AccountApplication(lambda: uow)

    row = await application.create_account("account-a", nickname="A", remark="primary")

    assert row.account_id == "account-a"
    repo.add.assert_awaited_once_with(
        "account-a",
        nickname="A",
        remark="primary",
        enabled=True,
    )
    uow.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reads_do_not_commit_and_desired_list_uses_repository_filters() -> None:
    repo = MagicMock()
    repo.get_by_account_id = AsyncMock(return_value=_record())
    repo.list = AsyncMock(return_value=[_record(desired_state="running")])
    uow = _uow(repo)
    application = AccountApplication(lambda: uow)

    row = await application.get_account("account-a")
    running = await application.list_desired_running_accounts()

    assert row is not None
    assert running[0].desired_state == "running"
    repo.list.assert_awaited_once_with(only_enabled=True, desired_state="running")
    uow.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutations_commit_only_when_repository_reports_a_change() -> None:
    repo = MagicMock()
    repo.set_enabled = AsyncMock(side_effect=[True, False])
    repo.set_desired_state = AsyncMock(return_value=True)
    repo.set_remark = AsyncMock(return_value=True)
    repo.delete = AsyncMock(return_value=True)
    uow = _uow(repo)
    application = AccountApplication(lambda: uow)

    assert await application.set_enabled("account-a", False)
    assert not await application.set_enabled("missing", False)
    assert await application.set_desired_state("account-a", "running")
    assert await application.set_remark("account-a", "updated")
    assert await application.delete_account("account-a")

    assert uow.commit.await_count == 4
