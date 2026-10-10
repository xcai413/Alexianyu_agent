"""常驻 daemon 状态的持久化操作。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update

from xianyu_agent.db import DaemonInstance, get_async_session

ACTIVE_STATUSES = ("starting", "running", "stopping")
# 实例租约:心跳每 10 秒一次,租约 60 秒;90 秒没心跳即视为过期(M1)。
INSTANCE_LEASE_TTL = timedelta(seconds=60)
HEARTBEAT_STALE_AFTER = timedelta(seconds=90)


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
            lease_until=now + INSTANCE_LEASE_TTL,
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
                select(DaemonInstance).where(DaemonInstance.instance_id == instance_id).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return DaemonControl(shutdown_requested=True, restart_requested=False)
        now = datetime.now(UTC)
        row.last_heartbeat_at = now
        # 心跳同时续租:租约没过期的实例,别的副本不得判它异常退出。
        row.lease_until = now + INSTANCE_LEASE_TTL
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
        stmt = select(DaemonInstance).where(DaemonInstance.instance_id == instance_id).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()


async def active_instances() -> Sequence[DaemonInstance]:
    async with get_async_session() as session:
        stmt = (
            select(DaemonInstance)
            .where(DaemonInstance.status.in_(ACTIVE_STATUSES))
            .order_by(DaemonInstance.started_at.desc())
        )
        return list((await session.execute(stmt)).scalars().all())


async def list_instances(*, limit: int = 100) -> Sequence[DaemonInstance]:
    """按启动时间列出实例历史;集群状态用它解释接管与滚动重启。"""
    async with get_async_session() as session:
        stmt = (
            select(DaemonInstance).order_by(DaemonInstance.started_at.desc()).limit(max(1, limit))
        )
        return list((await session.execute(stmt)).scalars().all())


async def mark_expired_instances(
    *,
    reason: str,
    now: datetime | None = None,
    stale_after: timedelta = HEARTBEAT_STALE_AFTER,
    exclude_instance_id: str | None = None,
) -> int:
    """只把「租约已过期」的活跃记录标记为异常退出(M1)。

    多副本下不能无条件清理:仍在续租的实例说明它还在跑,它的记录必须保留。M1 之前
    这里是无条件 UPDATE,第二个副本一启动就会把第一个副本标成异常退出。
    lease_until 为 NULL 的历史行(升级前的旧版本)只在心跳也已过期时才清理,避免
    灰度期间误伤仍在运行的旧版本进程。
    """
    current = now or datetime.now(UTC)
    stale_before = current - stale_after
    statement = (
        update(DaemonInstance)
        .where(
            DaemonInstance.status.in_(ACTIVE_STATUSES),
            or_(
                DaemonInstance.lease_until <= current,
                and_(
                    DaemonInstance.lease_until.is_(None),
                    DaemonInstance.last_heartbeat_at <= stale_before,
                ),
            ),
        )
        .values(status="error", stopped_at=current, last_error=reason)
    )
    if exclude_instance_id is not None:
        statement = statement.where(DaemonInstance.instance_id != exclude_instance_id)
    async with get_async_session() as session:
        result = await session.execute(statement)
        await session.commit()
        return int(result.rowcount or 0)


async def _update_instance(instance_id: str, **values: object) -> None:
    async with get_async_session() as session:
        await session.execute(
            update(DaemonInstance).where(DaemonInstance.instance_id == instance_id).values(**values)
        )
        await session.commit()
