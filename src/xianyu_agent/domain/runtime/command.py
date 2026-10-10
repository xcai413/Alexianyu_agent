"""账号级跨进程 Worker 命令。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update

from xianyu_agent.db import (
    Account,
    DaemonInstance,
    WorkerCommand,
    WorkerStatus,
    get_async_session,
)
from xianyu_agent.db.models import WorkerCommandStatus, WorkerDesiredState
from xianyu_agent.domain.account import risk as worker_risk

from .daemon import ACTIVE_STATUSES, HEARTBEAT_STALE_AFTER

ACTIONS = {"start", "stop", "restart"}
SEND_REPLY_ACTION = "send_reply"
DELIVER_ACTION = "deliver"
MAX_REPLY_LENGTH = 2000
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
                    select(WorkerStatus).where(
                        WorkerStatus.account_id.in_([a.id for a in accounts])
                    )
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
                select(WorkerCommand).where(WorkerCommand.command_id == command_id).limit(1)
            )
        ).scalar_one_or_none()


async def submit_send_reply(
    account_id: str,
    *,
    chat_id: str,
    text: str,
    requested_by: str = "web",
) -> WorkerCommand | None:
    """Queue one operator-authored reply for the daemon that owns the account.

    Unlike the pool start/stop actions this never changes ``desired_state``: a
    reply is a one-shot side effect that must run on the already-connected
    Worker, and must fail loudly when that Worker is not running.
    """
    content = text.strip()
    if not content:
        msg = "回复内容不能为空"
        raise ValueError(msg)
    if len(content) > MAX_REPLY_LENGTH:
        msg = f"回复内容超过 {MAX_REPLY_LENGTH} 字上限"
        raise ValueError(msg)
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return None
        if not account.enabled:
            msg = f"账号 {account_id} 已禁用"
            raise ValueError(msg)
        row = WorkerCommand(
            command_id=uuid.uuid4().hex,
            account_id=account.id,
            action=SEND_REPLY_ACTION,
            status=WorkerCommandStatus.PENDING,
            requested_by=requested_by,
            payload={"chat_id": chat_id, "text": content},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def submit_delivery(
    account_id: str,
    *,
    order_id: str,
    item_id: str | None = None,
    quantity: int | None = None,
    requested_by: str = "web",
) -> WorkerCommand | None:
    """Queue one real delivery for the daemon that owns the account.

    The daemon runs the phase-6 executor with the live Worker as its send port,
    so this never opens a competing WebSocket session. Like an operator reply,
    it does not touch desired_state.
    """
    if not order_id.strip():
        msg = "order_id 不能为空"
        raise ValueError(msg)
    if quantity is not None and quantity < 0:
        msg = "quantity 不能为负数"
        raise ValueError(msg)
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return None
        if not account.enabled:
            msg = f"账号 {account_id} 已禁用"
            raise ValueError(msg)
        row = WorkerCommand(
            command_id=uuid.uuid4().hex,
            account_id=account.id,
            action=DELIVER_ACTION,
            status=WorkerCommandStatus.PENDING,
            requested_by=requested_by,
            payload={
                "order_id": order_id.strip(),
                "item_id": item_id or None,
                "quantity": quantity or None,
            },
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


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
                status=(WorkerCommandStatus.SUCCEEDED if success else WorkerCommandStatus.FAILED),
                result=result,
                error=error,
                completed_at=now,
            )
        )
        await session.commit()


async def fail_stale_running(*, reason: str) -> int:
    """把「owner 已不存在或租约过期」的 running 命令置失败(M1)。

    仍在续租的实例说明它还在执行该命令:多副本下别的副本不能把它的命令判失败,
    否则会出现「A 正在执行、B 把它标记成失败并重做」的重复执行。
    """
    current = datetime.now(UTC)
    stale_before = current - HEARTBEAT_STALE_AFTER
    live_instances = select(DaemonInstance.instance_id).where(
        DaemonInstance.status.in_(ACTIVE_STATUSES),
        or_(
            DaemonInstance.lease_until > current,
            and_(
                DaemonInstance.lease_until.is_(None),
                DaemonInstance.last_heartbeat_at > stale_before,
            ),
        ),
    )
    async with get_async_session() as session:
        result = await session.execute(
            update(WorkerCommand)
            .where(WorkerCommand.status == WorkerCommandStatus.RUNNING)
            .where(
                or_(
                    WorkerCommand.daemon_instance_id.is_(None),
                    WorkerCommand.daemon_instance_id.not_in(live_instances),
                )
            )
            .values(
                status=WorkerCommandStatus.FAILED,
                error=reason,
                completed_at=current,
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
