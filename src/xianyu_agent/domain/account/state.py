"""Account compatibility facade plus legacy worker-status persistence helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.application.ports.repositories import AccountRecord
from xianyu_agent.composition import get_account_application
from xianyu_agent.db import get_async_session
from xianyu_agent.infrastructure.database.models import Account, WorkerStatus


async def create_account(
    account_id: str,
    *,
    nickname: str | None = None,
    remark: str | None = None,
    enabled: bool = True,
) -> AccountRecord:
    """Create an account through the application/UoW boundary."""
    return await get_account_application().create_account(
        account_id,
        nickname=nickname,
        remark=remark,
        enabled=enabled,
    )


async def get_account(account_id: str) -> AccountRecord | None:
    return await get_account_application().get_account(account_id)


async def list_accounts(*, only_enabled: bool = False) -> Sequence[AccountRecord]:
    return await get_account_application().list_accounts(only_enabled=only_enabled)


async def list_desired_running_accounts() -> Sequence[AccountRecord]:
    """列出允许运行且期望在线的账号。"""
    return await get_account_application().list_desired_running_accounts()


async def set_enabled(account_id: str, enabled: bool) -> bool:
    """Enable/disable an account. Returns False if account missing."""
    return await get_account_application().set_enabled(account_id, enabled)


async def set_desired_state(account_id: str, desired_state: str) -> bool:
    """设置 daemon 中账号 Worker 的期望状态。"""
    return await get_account_application().set_desired_state(account_id, desired_state)


async def set_remark(account_id: str, remark: str | None) -> bool:
    """Update the optional operator remark for an account."""
    return await get_account_application().set_remark(account_id, remark)


async def delete_account(account_id: str) -> bool:
    """Delete an account and cascade its rows. Returns False if missing."""
    return await get_account_application().delete_account(account_id)


async def worker_statuses() -> Sequence[WorkerStatus]:
    """Latest worker heartbeat rows for all accounts."""
    async with get_async_session() as session:
        stmt = select(WorkerStatus).order_by(WorkerStatus.updated_at.desc())
        return list((await session.execute(stmt)).scalars().all())


async def worker_status_for(account_id: str) -> WorkerStatus | None:
    async with get_async_session() as session:
        account = await _get_account_model(session, account_id)
        if account is None:
            return None
        return (
            await session.execute(
                select(WorkerStatus).where(WorkerStatus.account_id == account.id).limit(1)
            )
        ).scalar_one_or_none()


async def mark_worker_offline(account_id: str, *, reason: str = "CLI 停止") -> None:
    """Best-effort remote stop marker: set worker_status offline without a live process."""
    async with get_async_session() as session:
        account = await _get_account_model(session, account_id)
        if account is None:
            return
        row = (
            await session.execute(
                select(WorkerStatus).where(WorkerStatus.account_id == account.id).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = WorkerStatus(account_id=account.id, status="offline")
            session.add(row)
        else:
            row.status = "offline"
            row.last_error = reason
        account.last_heartbeat_at = datetime.now(UTC)
        await session.commit()


async def _get_account_model(session: AsyncSession, account_id: str) -> Account | None:
    return (
        await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
    ).scalar_one_or_none()
