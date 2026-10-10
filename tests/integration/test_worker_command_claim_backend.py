"""Worker 命令 CAS 抢占的跨数据库契约。"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import Account, WorkerCommand, get_async_session
from xianyu_agent.db.models import WorkerCommandStatus
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.runtime import command as worker_commands

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_worker_command_single_claim_across_backend() -> None:
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend

    suffix = uuid.uuid4().hex[:12]
    account_id = f"command-{backend}-{suffix}"
    await domain_accounts.create_account(account_id, enabled=False)
    async with get_async_session() as session:
        account = await session.scalar(select(Account).where(Account.account_id == account_id))
        assert account is not None
        command = WorkerCommand(
            command_id=uuid.uuid4().hex,
            account_id=account.id,
            action="stop",
            status=WorkerCommandStatus.PENDING,
            requested_by="backend-contract",
        )
        session.add(command)
        await session.commit()
        command_id = command.command_id

    winners = await asyncio.gather(
        worker_commands.claim(command_id, daemon_instance_id="replica-a"),
        worker_commands.claim(command_id, daemon_instance_id="replica-b"),
    )
    assert sum(winners) == 1
    stored = await worker_commands.get(command_id)
    assert stored is not None
    assert stored.status == WorkerCommandStatus.RUNNING
    assert stored.daemon_instance_id in {"replica-a", "replica-b"}
