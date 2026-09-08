"""常驻 daemon 状态的持久化操作。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update

from xianyu_agent.db import DaemonInstance, get_async_session

ACTIVE_STATUSES = ("starting", "running", "stopping")


class DaemonControl:
    def __init__(self, *, shutdown_requested: bool, restart_requested: bool) -> None:
        self.shutdown_requested = shutdown_requested
        self.restart_requested = restart_requested


async def create_instance(*, instance_id: str, pid: int, version: str) -> DaemonInstance:
    """创建一条新的 daemon 运行记录。"""
    now = datetime.now(UTC)
    async with get_async_session() as session:
        row = DaemonInstance(
            instance_id=instance_id,
            pid=pid,
            status="starting",
            version=version,
            started_at=now,
            last_heartbeat_at=now,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def mark_running(instance_id: str) -> None:
    await _update_instance(instance_id, status="running", last_error=None)


async def touch_heartbeat(instance_id: str) -> DaemonControl:
    """刷新心跳并返回持久化控制标记。"""
    async with get_async_session() as session:
        row = (
            await session.execute(
                select(DaemonInstance)
                .where(DaemonInstance.instance_id == instance_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return DaemonControl(shutdown_requested=True, restart_requested=False)
        row.last_heartbeat_at = datetime.now(UTC)
        await session.commit()
        return DaemonControl(
            shutdown_requested=bool(row.shutdown_requested),
            restart_requested=bool(row.restart_requested),
        )


async def mark_stopping(instance_id: str) -> None:
    await _update_instance(instance_id, status="stopping")


async def mark_stopped(instance_id: str, *, error: str | None = None) -> None:
    await _update_instance(
        instance_id,
        status="error" if error else "stopped",
        stopped_at=datetime.now(UTC),
        last_error=error,
    )


async def request_shutdown(instance_id: str | None = None) -> int:
    """请求一个或全部活跃 daemon 有序停止。"""
    async with get_async_session() as session:
        stmt = (
            update(DaemonInstance)
            .where(DaemonInstance.status.in_(ACTIVE_STATUSES))
            .values(shutdown_requested=True)
        )
        if instance_id is not None:
            stmt = stmt.where(DaemonInstance.instance_id == instance_id)
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount or 0)


async def request_restart(instance_id: str | None = None) -> int:
    """请求一个或全部活跃 daemon 有序重启。"""
    async with get_async_session() as session:
        stmt = (
            update(DaemonInstance)
            .where(DaemonInstance.status.in_(ACTIVE_STATUSES))
            .values(shutdown_requested=True, restart_requested=True)
        )
        if instance_id is not None:
            stmt = stmt.where(DaemonInstance.instance_id == instance_id)
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount or 0)


async def latest_instance() -> DaemonInstance | None:
    async with get_async_session() as session:
        stmt = select(DaemonInstance).order_by(DaemonInstance.started_at.desc()).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()


async def get_instance(instance_id: str) -> DaemonInstance | None:
    async with get_async_session() as session:
        stmt = (
            select(DaemonInstance)
            .where(DaemonInstance.instance_id == instance_id)
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def active_instances() -> Sequence[DaemonInstance]:
    async with get_async_session() as session:
        stmt = (
            select(DaemonInstance)
            .where(DaemonInstance.status.in_(ACTIVE_STATUSES))
            .order_by(DaemonInstance.started_at.desc())
        )
        return list((await session.execute(stmt)).scalars().all())


async def mark_orphaned_active(*, reason: str) -> int:
    """持有单实例锁后,将数据库中遗留的活跃记录标记为异常退出。"""
    now = datetime.now(UTC)
    async with get_async_session() as session:
        result = await session.execute(
            update(DaemonInstance)
            .where(DaemonInstance.status.in_(ACTIVE_STATUSES))
            .values(status="error", stopped_at=now, last_error=reason)
        )
        await session.commit()
        return int(result.rowcount or 0)


async def _update_instance(instance_id: str, **values: object) -> None:
    async with get_async_session() as session:
        await session.execute(
            update(DaemonInstance)
            .where(DaemonInstance.instance_id == instance_id)
            .values(**values)
        )
        await session.commit()
