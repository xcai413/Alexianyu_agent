"""账号级跨进程 Worker 命令。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update

from xianyu_agent.db import Account, WorkerCommand, WorkerStatus, get_async_session
from xianyu_agent.db.models import WorkerCommandStatus, WorkerDesiredState
from xianyu_agent.domain import worker_risk

ACTIONS = {"start", "stop", "restart"}
TERMINAL_STATUSES = {WorkerCommandStatus.SUCCEEDED, WorkerCommandStatus.FAILED}


async def submit(
    account_id: str,
    action: str,
    *,
    requested_by: str = "cli",
) -> WorkerCommand | None:
    """原子更新期望状态并创建 pending 命令。"""
    if action not in ACTIONS:
        msg = f"invalid worker action: {action}"
        raise ValueError(msg)
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return None
        if action in {"start", "restart"} and not account.enabled:
            msg = f"账号 {account_id} 已禁用"
            raise ValueError(msg)
        if action in {"start", "restart"}:
            blocked = await worker_risk.start_block_reason(account_id)
            if blocked:
                raise ValueError(blocked)
        account.desired_state = (
            WorkerDesiredState.STOPPED if action == "stop" else WorkerDesiredState.RUNNING
        )
        row = WorkerCommand(
            command_id=uuid.uuid4().hex,
            account_id=account.id,
            action=action,
            status=WorkerCommandStatus.PENDING,
            requested_by=requested_by,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def submit_many(
    action: str,
    *,
    requested_by: str = "cli",
) -> list[WorkerCommand]:
    """在一个事务中为所有启用账号提交同一动作。"""
    if action not in ACTIONS:
        msg = f"invalid worker action: {action}"
        raise ValueError(msg)
    async with get_async_session() as session:
        accounts = list(
            (
                await session.execute(
                    select(Account)
                    .where(Account.enabled.is_(True))
                    .order_by(Account.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        desired_state = (
            WorkerDesiredState.STOPPED if action == "stop" else WorkerDesiredState.RUNNING
        )
        rows: list[WorkerCommand] = []
        risk_rows = {
            row.account_id: row
            for row in (
                await session.execute(
                    select(WorkerStatus).where(WorkerStatus.account_id.in_([a.id for a in accounts]))
                )
            )
            .scalars()
            .all()
        }
        for account in accounts:
            if action in {"start", "restart"}:
                risk = risk_rows.get(account.id)
                if risk is not None and risk.risk_recovery_required:
                    continue
            account.desired_state = desired_state
            row = WorkerCommand(
                command_id=uuid.uuid4().hex,
                account_id=account.id,
                action=action,
                status=WorkerCommandStatus.PENDING,
                requested_by=requested_by,
            )
            session.add(row)
            rows.append(row)
        await session.commit()
        for row in rows:
            await session.refresh(row)
        return rows


async def get(command_id: str) -> WorkerCommand | None:
    async with get_async_session() as session:
        return (
            await session.execute(
                select(WorkerCommand)
                .where(WorkerCommand.command_id == command_id)
                .limit(1)
            )
        ).scalar_one_or_none()


async def pending(*, limit: int = 100) -> Sequence[WorkerCommand]:
    async with get_async_session() as session:
        stmt = (
            select(WorkerCommand)
            .where(WorkerCommand.status == WorkerCommandStatus.PENDING)
            .order_by(WorkerCommand.requested_at.asc(), WorkerCommand.id.asc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())


async def claim(command_id: str, *, daemon_instance_id: str) -> bool:
    """仅 pending 命令可被一个 daemon 原子领取。"""
    async with get_async_session() as session:
        result = await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.command_id == command_id)
            .where(WorkerCommand.status == WorkerCommandStatus.PENDING)
            .values(
                status=WorkerCommandStatus.RUNNING,
                daemon_instance_id=daemon_instance_id,
                claimed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        return bool(result.rowcount)


async def complete(
    command_id: str,
    *,
    success: bool,
    result: str | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    async with get_async_session() as session:
        await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.command_id == command_id)
            .where(WorkerCommand.status == WorkerCommandStatus.RUNNING)
            .values(
                status=(
                    WorkerCommandStatus.SUCCEEDED if success else WorkerCommandStatus.FAILED
                ),
                result=result,
                error=error,
                completed_at=now,
            )
        )
        await session.commit()


async def fail_stale_running(*, reason: str) -> int:
    """daemon 接管时把旧实例遗留的 running 命令置失败。"""
    async with get_async_session() as session:
        result = await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.status == WorkerCommandStatus.RUNNING)
            .values(
                status=WorkerCommandStatus.FAILED,
                error=reason,
                completed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        return int(result.rowcount or 0)

async def account_key(command: WorkerCommand) -> str | None:
    async with get_async_session() as session:
        return (
            await session.execute(
                select(Account.account_id).where(Account.id == command.account_id).limit(1)
            )
        ).scalar_one_or_none()
