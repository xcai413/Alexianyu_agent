"""Account management helpers (shared by CLI, pool, and future MCP tools)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from xianyu_agent.db import Account, WorkerStatus, get_async_session
from xianyu_agent.db.models import WorkerDesiredState


async def create_account(
    account_id: str,
    *,
    nickname: str | None = None,
    remark: str | None = None,
    enabled: bool = True,
) -> Account:
    """Create an account row. Idempotent: returns existing row if present."""
    async with get_async_session() as session:
        stmt = select(Account).where(Account.account_id == account_id).limit(1)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row
        row = Account(
            account_id=account_id,
            nickname=nickname,
            remark=remark,
            enabled=enabled,
            desired_state=WorkerDesiredState.STOPPED,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def get_account(account_id: str) -> Account | None:
    async with get_async_session() as session:
        return (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()


async def list_accounts(*, only_enabled: bool = False) -> Sequence[Account]:
    async with get_async_session() as session:
        stmt = select(Account).order_by(Account.created_at.desc())
        if only_enabled:
            stmt = stmt.where(Account.enabled.is_(True))
        return list((await session.execute(stmt)).scalars().all())


async def list_desired_running_accounts() -> Sequence[Account]:
    """列出允许运行且期望在线的账号。"""
    async with get_async_session() as session:
        stmt = (
            select(Account)
            .where(Account.enabled.is_(True))
            .where(Account.desired_state == WorkerDesiredState.RUNNING)
            .order_by(Account.created_at.desc())
        )
        return list((await session.execute(stmt)).scalars().all())


async def set_enabled(account_id: str, enabled: bool) -> bool:
    """Enable/disable an account. Returns False if account missing."""
    async with get_async_session() as session:
        row = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.enabled = enabled
        if not enabled:
            row.desired_state = WorkerDesiredState.STOPPED
        await session.commit()
        return True


async def set_desired_state(account_id: str, desired_state: str) -> bool:
    """设置 daemon 中账号 Worker 的期望状态。"""
    if desired_state not in {WorkerDesiredState.RUNNING, WorkerDesiredState.STOPPED}:
        msg = f"invalid desired_state: {desired_state}"
        raise ValueError(msg)
    async with get_async_session() as session:
        row = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.desired_state = desired_state
        await session.commit()
        return True


async def delete_account(account_id: str) -> bool:
    """Delete an account and cascade its rows. Returns False if missing."""
    async with get_async_session() as session:
        row = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def worker_statuses() -> Sequence[WorkerStatus]:
    """Latest worker heartbeat rows for all accounts."""
    async with get_async_session() as session:
        stmt = select(WorkerStatus).order_by(WorkerStatus.updated_at.desc())
        return list((await session.execute(stmt)).scalars().all())


async def worker_status_for(account_id: str) -> WorkerStatus | None:
    async with get_async_session() as session:
        account = await get_account(account_id)
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
        account = await get_account(account_id)
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
