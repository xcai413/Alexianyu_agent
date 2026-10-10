"""账号归属租约(M2):同一账号在任一时刻最多一个未过期 owner。

多副本下"谁持有账号"不能靠进程内状态或文件锁:文件锁只在同一台机器有效。这里用
accounts 行上的租约列做 CAS 抢占,与 scheduler_work / transactional_outbox 同一套路。

没有设置实例身份的调用方(单机 CLI)不参与租约:它们既不写 owner,也不被 owner 拦。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from xianyu_agent.db import Account, AuditLog, get_async_session
from xianyu_agent.domain.account import risk as worker_risk

ACCOUNT_LEASE_TTL = timedelta(seconds=60)
# 接管冷却:owner 租约过期后还要再等这么久才允许别的实例接管,避免网络抖动导致的双会话。
ACCOUNT_TAKEOVER_COOLDOWN = timedelta(seconds=120)
TAKEOVER_FAILURE_LIMIT_CODE = "TAKEOVER_FAILURE_LIMIT"


@dataclass(frozen=True)
class AccountLease:
    """账号当前归属。owner 为空表示没有实例持有。"""

    account_id: str
    owner_instance_id: str | None
    lease_until: datetime | None
    heartbeat_at: datetime | None
    takeover_approved_at: datetime | None = None
    takeover_approved_by: str | None = None
    takeover_failure_count: int = 0
    takeover_last_failure_at: datetime | None = None

    def is_live(self, *, now: datetime | None = None) -> bool:
        if self.owner_instance_id is None or self.lease_until is None:
            return False
        return _as_utc(self.lease_until) > _as_utc(now or datetime.now(UTC))

    def remaining_seconds(self, *, now: datetime | None = None) -> float:
        if self.lease_until is None:
            return 0.0
        current = _as_utc(now or datetime.now(UTC))
        return max(0.0, (_as_utc(self.lease_until) - current).total_seconds())


@dataclass(frozen=True)
class LeaseClaimResult:
    """一次启动前租约判定,包含是否属于旧 owner 的接管。"""

    acquired: bool
    takeover: bool = False
    previous_owner: str | None = None
    reason: str = "acquired"


async def get(account_id: str) -> AccountLease | None:
    async with get_async_session() as session:
        row = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if row is None:
            return None
        return AccountLease(
            account_id=row.account_id,
            owner_instance_id=row.owner_instance_id,
            lease_until=row.owner_lease_until,
            heartbeat_at=row.owner_heartbeat_at,
            takeover_approved_at=row.takeover_approved_at,
            takeover_approved_by=row.takeover_approved_by,
            takeover_failure_count=int(row.takeover_failure_count or 0),
            takeover_last_failure_at=row.takeover_last_failure_at,
        )


async def live_owner(account_id: str, *, now: datetime | None = None) -> str | None:
    """账号当前是否有未过期 owner;没有则返回 None(CLI 前置检查用)。"""
    lease = await get(account_id)
    if lease is None or not lease.is_live(now=now):
        return None
    return lease.owner_instance_id


async def claim(
    account_id: str,
    *,
    instance_id: str,
    ttl: timedelta = ACCOUNT_LEASE_TTL,
    cooldown: timedelta = ACCOUNT_TAKEOVER_COOLDOWN,
    now: datetime | None = None,
) -> bool:
    """抢占账号归属;只有空闲、自己持有、或对方租约过期超过冷却期时才成功。"""
    result = await claim_for_start(
        account_id,
        instance_id=instance_id,
        ttl=ttl,
        cooldown=cooldown,
        now=now,
    )
    return result.acquired


async def claim_for_start(
    account_id: str,
    *,
    instance_id: str,
    ttl: timedelta = ACCOUNT_LEASE_TTL,
    cooldown: timedelta = ACCOUNT_TAKEOVER_COOLDOWN,
    require_approval: bool = False,
    now: datetime | None = None,
) -> LeaseClaimResult:
    """启动前 CAS 抢占,并执行接管批准门禁。

    空 owner 的首次分配不是接管。旧 owner 不同或已有未完成接管失败计数时,本次属于
    接管序列;人工模式下整个序列共用一次批准,直到成功或熔断才清除。
    """
    if not instance_id:
        msg = "instance_id must not be empty"
        raise ValueError(msg)
    current = _as_utc(now or datetime.now(UTC))
    takeover_before = current - cooldown
    async with get_async_session() as session:
        previous = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if previous is None:
            return LeaseClaimResult(acquired=False, reason="account_not_found")
        previous_owner = previous.owner_instance_id
        takeover = bool(
            (previous_owner is not None and previous_owner != instance_id)
            or previous.takeover_failure_count
        )
        lease_until = previous.owner_lease_until
        eligible = (
            previous_owner is None
            or previous_owner == instance_id
            or lease_until is None
            or _as_utc(lease_until) <= takeover_before
        )
        if not eligible:
            return LeaseClaimResult(
                acquired=False,
                takeover=takeover,
                previous_owner=previous_owner,
                reason="owned_by_other_instance",
            )
        if takeover and require_approval and previous.takeover_approved_at is None:
            return LeaseClaimResult(
                acquired=False,
                takeover=True,
                previous_owner=previous_owner,
                reason="takeover_approval_required",
            )
        conditions = [
            or_(
                Account.owner_instance_id.is_(None),
                Account.owner_instance_id == instance_id,
                Account.owner_lease_until.is_(None),
                Account.owner_lease_until <= takeover_before,
            )
        ]
        if takeover and require_approval:
            conditions.append(Account.takeover_approved_at.is_not(None))
        result = await session.execute(
            update(Account)
            .where(Account.account_id == account_id)
            .where(*conditions)
            .values(
                owner_instance_id=instance_id,
                owner_lease_until=current + ttl,
                owner_heartbeat_at=current,
            )
            # SQLite 读出的 DateTime 是 naive;CAS 判定交给数据库,避免在 Python 侧
            # 比较 naive/aware 抛 TypeError,也避免改动已加载的 ORM 对象。
            .execution_options(synchronize_session=False)
        )
        changed = bool(result.rowcount)
        if changed and previous_owner != instance_id:
            session.add(
                AuditLog(
                    actor="system",
                    action="account_lease_acquired",
                    target=account_id,
                    params={
                        "owner": instance_id,
                        "previous_owner": previous_owner,
                        "ttl_seconds": int(ttl.total_seconds()),
                        "takeover": takeover,
                    },
                    result="ok",
                )
            )
        await session.commit()
        return LeaseClaimResult(
            acquired=changed,
            takeover=takeover if changed else False,
            previous_owner=previous_owner,
            reason="acquired" if changed else "claim_raced",
        )


async def approve_takeover(
    account_id: str,
    *,
    actor: str = "cli",
    now: datetime | None = None,
) -> bool:
    """批准该账号下一段接管序列;批准在成功或熔断时自动消费。"""
    approved_at = _as_utc(now or datetime.now(UTC))
    async with get_async_session() as session:
        result = await session.execute(
            update(Account)
            .where(Account.account_id == account_id)
            .values(takeover_approved_at=approved_at, takeover_approved_by=actor)
            .execution_options(synchronize_session=False)
        )
        changed = bool(result.rowcount)
        if changed:
            session.add(
                AuditLog(
                    actor=actor,
                    action="account_takeover_approved",
                    target=account_id,
                    params={"approved_at": approved_at.isoformat()},
                    result="ok",
                )
            )
        await session.commit()
        return changed


async def revoke_takeover(account_id: str, *, actor: str = "cli") -> bool:
    """撤销尚未成功的人工接管批准。"""
    async with get_async_session() as session:
        result = await session.execute(
            update(Account)
            .where(Account.account_id == account_id)
            .where(Account.takeover_approved_at.is_not(None))
            .values(takeover_approved_at=None, takeover_approved_by=None)
            .execution_options(synchronize_session=False)
        )
        changed = bool(result.rowcount)
        if changed:
            session.add(
                AuditLog(
                    actor=actor,
                    action="account_takeover_revoked",
                    target=account_id,
                    params={},
                    result="ok",
                )
            )
        await session.commit()
        return changed


async def record_takeover_success(account_id: str, *, instance_id: str) -> bool:
    """接管 Worker 启动成功:连续失败归零并消费人工批准。"""
    async with get_async_session() as session:
        result = await session.execute(
            update(Account)
            .where(Account.account_id == account_id)
            .where(Account.owner_instance_id == instance_id)
            .values(
                takeover_approved_at=None,
                takeover_approved_by=None,
                takeover_failure_count=0,
                takeover_last_failure_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        changed = bool(result.rowcount)
        if changed:
            session.add(
                AuditLog(
                    actor="system",
                    action="account_takeover_succeeded",
                    target=account_id,
                    params={"owner": instance_id},
                    result="ok",
                )
            )
        await session.commit()
        return changed


async def record_takeover_failure(
    account_id: str,
    *,
    instance_id: str,
    max_failures: int,
    error_type: str,
    now: datetime | None = None,
) -> int:
    """记录接管启动失败并释放租约;达到上限后打开现有人工恢复闸门。"""
    if max_failures < 1:
        msg = "max_failures must be positive"
        raise ValueError(msg)
    failed_at = _as_utc(now or datetime.now(UTC))
    async with get_async_session() as session:
        result = await session.execute(
            update(Account)
            .where(Account.account_id == account_id)
            .where(Account.owner_instance_id == instance_id)
            .values(
                owner_instance_id=None,
                owner_lease_until=None,
                owner_heartbeat_at=None,
                takeover_failure_count=Account.takeover_failure_count + 1,
                takeover_last_failure_at=failed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            await session.rollback()
            return 0
        failures = int(
            (
                await session.execute(
                    select(Account.takeover_failure_count).where(Account.account_id == account_id)
                )
            ).scalar_one()
        )
        tripped = failures >= max_failures
        if tripped:
            await session.execute(
                update(Account)
                .where(Account.account_id == account_id)
                .values(takeover_approved_at=None, takeover_approved_by=None)
                .execution_options(synchronize_session=False)
            )
        session.add(
            AuditLog(
                actor="system",
                action="account_takeover_failed",
                target=account_id,
                params={
                    "owner": instance_id,
                    "failure_count": failures,
                    "max_failures": max_failures,
                    "error_type": error_type,
                },
                result="blocked" if tripped else "retry",
                error=error_type,
            )
        )
        await session.commit()
    if tripped:
        await worker_risk.open_user_validate(account_id, code=TAKEOVER_FAILURE_LIMIT_CODE)
    return failures


async def renew(
    account_id: str,
    *,
    instance_id: str,
    ttl: timedelta = ACCOUNT_LEASE_TTL,
    now: datetime | None = None,
) -> bool:
    """续租只有当前 owner 能成功;租约即使已过期也仍由 owner 续回(不会误接管自己)。"""
    if not instance_id:
        msg = "instance_id must not be empty"
        raise ValueError(msg)
    current = _as_utc(now or datetime.now(UTC))
    async with get_async_session() as session:
        result = await session.execute(
            update(Account)
            .where(Account.account_id == account_id)
            .where(Account.owner_instance_id == instance_id)
            .values(owner_lease_until=current + ttl, owner_heartbeat_at=current)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return bool(result.rowcount)


async def renew_owned(
    instance_id: str,
    *,
    ttl: timedelta = ACCOUNT_LEASE_TTL,
    now: datetime | None = None,
) -> int:
    """批量续租本实例持有的全部账号,返回续租条数。"""
    if not instance_id:
        msg = "instance_id must not be empty"
        raise ValueError(msg)
    current = _as_utc(now or datetime.now(UTC))
    async with get_async_session() as session:
        result = await session.execute(
            update(Account)
            .where(Account.owner_instance_id == instance_id)
            .values(owner_lease_until=current + ttl, owner_heartbeat_at=current)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return int(result.rowcount or 0)


async def release(account_id: str, *, instance_id: str) -> bool:
    """主动释放归属(停止 Worker / 账号不再期望运行时调用)。"""
    if not instance_id:
        msg = "instance_id must not be empty"
        raise ValueError(msg)
    async with get_async_session() as session:
        result = await session.execute(
            update(Account)
            .where(Account.account_id == account_id)
            .where(Account.owner_instance_id == instance_id)
            .values(owner_instance_id=None, owner_lease_until=None, owner_heartbeat_at=None)
            .execution_options(synchronize_session=False)
        )
        changed = bool(result.rowcount)
        if changed:
            session.add(
                AuditLog(
                    actor="system",
                    action="account_lease_released",
                    target=account_id,
                    params={"owner": instance_id},
                    result="ok",
                )
            )
        await session.commit()
        return changed


async def release_owned(instance_id: str) -> int:
    """优雅停机时释放本副本全部账号,让其他副本无需等待 TTL 与冷却。"""
    if not instance_id:
        msg = "instance_id must not be empty"
        raise ValueError(msg)
    async with get_async_session() as session:
        account_ids = list(
            (
                await session.execute(
                    select(Account.account_id).where(Account.owner_instance_id == instance_id)
                )
            )
            .scalars()
            .all()
        )
        if not account_ids:
            return 0
        result = await session.execute(
            update(Account)
            .where(Account.owner_instance_id == instance_id)
            .values(owner_instance_id=None, owner_lease_until=None, owner_heartbeat_at=None)
            .execution_options(synchronize_session=False)
        )
        for account_id in account_ids:
            session.add(
                AuditLog(
                    actor="system",
                    action="account_lease_released",
                    target=account_id,
                    params={"owner": instance_id, "reason": "graceful_shutdown"},
                    result="ok",
                )
            )
        await session.commit()
        return int(result.rowcount or 0)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
