"""账号 Worker 验证风险的持久化熔断状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from xianyu_agent.db import Account, AuditLog, WorkerStatus, get_async_session
from xianyu_agent.db.models import WorkerDesiredState

USER_VALIDATE_CODE = "FAIL_SYS_USER_VALIDATE"
USER_VALIDATE_COOLDOWN = timedelta(minutes=20)
RISK_COOLING_STATUS = "risk_cooling"


@dataclass(frozen=True)
class WorkerRiskCircuit:
    """一个账号当前的人工恢复闸门。"""

    account_id: str
    code: str
    detected_at: datetime | None
    cooldown_until: datetime | None
    recovery_required: bool

    def is_cooling(self, *, now: datetime | None = None) -> bool:
        if self.cooldown_until is None:
            return False
        current = now or datetime.now(UTC)
        return _as_utc(self.cooldown_until) > current.astimezone(UTC)

    def remaining_seconds(self, *, now: datetime | None = None) -> float:
        if self.cooldown_until is None:
            return 0.0
        current = now or datetime.now(UTC)
        return max(0.0, (_as_utc(self.cooldown_until) - current.astimezone(UTC)).total_seconds())


def is_user_validate_error(error: BaseException | str) -> bool:
    """仅匹配闲鱼明确返回的人工验证码,避免误伤普通认证错误。"""
    return USER_VALIDATE_CODE in str(error).upper()


async def get(account_id: str) -> WorkerRiskCircuit | None:
    """读取账号的未解除验证熔断;没有熔断时返回 None。"""
    async with get_async_session() as session:
        row = await _select_status(session, account_id)
        if row is None or not row.risk_code or not row.risk_recovery_required:
            return None
        return WorkerRiskCircuit(
            account_id=account_id,
            code=row.risk_code,
            detected_at=row.risk_detected_at,
            cooldown_until=row.risk_cooldown_until,
            recovery_required=bool(row.risk_recovery_required),
        )


async def open_user_validate(account_id: str, *, now: datetime | None = None) -> WorkerRiskCircuit | None:
    """打开人工验证熔断,并停止该账号的 daemon 期望状态。"""
    detected_at = (now or datetime.now(UTC)).astimezone(UTC)
    cooldown_until = detected_at + USER_VALIDATE_COOLDOWN
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return None
        row = (
            await session.execute(
                select(WorkerStatus).where(WorkerStatus.account_id == account.id).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = WorkerStatus(account_id=account.id)
            session.add(row)
        account.desired_state = WorkerDesiredState.STOPPED
        row.status = RISK_COOLING_STATUS
        row.last_error = USER_VALIDATE_CODE
        row.risk_code = USER_VALIDATE_CODE
        row.risk_detected_at = detected_at
        row.risk_cooldown_until = cooldown_until
        row.risk_recovery_required = True
        session.add(
            AuditLog(
                actor="system",
                action="ws_user_validate_circuit_opened",
                target=account_id,
                params={
                    "risk_code": USER_VALIDATE_CODE,
                    "cooldown_seconds": int(USER_VALIDATE_COOLDOWN.total_seconds()),
                },
                result="blocked",
                error=USER_VALIDATE_CODE,
            )
        )
        await session.commit()
    return WorkerRiskCircuit(
        account_id=account_id,
        code=USER_VALIDATE_CODE,
        detected_at=detected_at,
        cooldown_until=cooldown_until,
        recovery_required=True,
    )


async def clear_after_refresh(account_id: str) -> bool:
    """仅在一次人工触发的 Token 刷新成功后解除熔断。"""
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return False
        row = (
            await session.execute(
                select(WorkerStatus).where(WorkerStatus.account_id == account.id).limit(1)
            )
        ).scalar_one_or_none()
        if row is None or not row.risk_recovery_required:
            return False
        previous_code = row.risk_code
        row.status = "offline"
        row.last_error = None
        row.risk_code = None
        row.risk_detected_at = None
        row.risk_cooldown_until = None
        row.risk_recovery_required = False
        session.add(
            AuditLog(
                actor="system",
                action="ws_user_validate_circuit_cleared",
                target=account_id,
                params={"risk_code": previous_code or USER_VALIDATE_CODE},
                result="ok",
            )
        )
        await session.commit()
        return True


async def start_block_reason(account_id: str) -> str | None:
    """返回启动阻断原因;解除前始终要求先执行成功的人工刷新。"""
    circuit = await get(account_id)
    if circuit is None:
        return None
    if circuit.is_cooling():
        return (
            f"账号 {account_id} 处于 {circuit.code} 验证冷却;"
            "冷却结束后先执行 auth refresh。"
        )
    return f"账号 {account_id} 需要人工验证恢复;请先执行 auth refresh。"


async def _select_status(session, account_id: str) -> WorkerStatus | None:
    return (
        await session.execute(
            select(WorkerStatus)
            .join(Account, Account.id == WorkerStatus.account_id)
            .where(Account.account_id == account_id)
            .limit(1)
        )
    ).scalar_one_or_none()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
