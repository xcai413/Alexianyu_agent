"""账号 Worker 验证风险的持久化熔断状态。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, select, update

from xianyu_agent.db import Account, AuditLog, WorkerStatus, get_async_session
from xianyu_agent.db.models import WorkerDesiredState
from xianyu_agent.services.logging_setup import redact_text

USER_VALIDATE_CODE = "FAIL_SYS_USER_VALIDATE"
# 冷却按连续命中递增:第一次只停一分钟,继续命中才逐步拉长到 20 分钟。参考实现的实测是
# "一开始就锁一小时"会把正常账号白白锁住,而完全不递增又扛不住反复命中。
COOLDOWN_STEPS_SECONDS: tuple[int, ...] = (60, 180, 300, 600, 1200)
USER_VALIDATE_COOLDOWN = timedelta(seconds=COOLDOWN_STEPS_SECONDS[-1])
RISK_COOLING_STATUS = "risk_cooling"
# 平台把"别再发了"用几种不同的字面量表达。只认其中一个会漏掉其余表达,漏掉的代价是
# 继续按原节奏重试——参考项目记录过重试风暴会让封控从分钟级延长到小时级。
RISK_CONTROL_MARKERS = (
    USER_VALIDATE_CODE,
    "RGV587_ERROR",
    "FAIL_SYS_FLOW_LIMIT",
    "被挤爆",
    "哎哟喂",
    "SM::",
)


@dataclass(frozen=True)
class WorkerRiskCircuit:
    """一个账号当前的人工恢复闸门。"""

    account_id: str
    code: str
    detected_at: datetime | None
    cooldown_until: datetime | None
    recovery_required: bool
    revision: str | None = None

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
    """Whether the platform told us to stop: verification or flow limiting.

    Covers the whole observed risk family, not just FAIL_SYS_USER_VALIDATE, while
    still requiring an explicit platform marker so ordinary auth errors are not
    misread as risk control.
    """
    text = str(error).upper()
    return any(marker.upper() in text for marker in RISK_CONTROL_MARKERS)


def detected_risk_code(error: BaseException | str) -> str:
    """The first risk marker found, for accurate audit and persisted state."""
    text = str(error).upper()
    for marker in RISK_CONTROL_MARKERS:
        if marker.upper() in text:
            return marker
    return USER_VALIDATE_CODE


def cooldown_for(hits: int) -> timedelta:
    """Cooldown for the n-th consecutive risk hit, capped at the longest step."""
    index = min(max(hits, 1), len(COOLDOWN_STEPS_SECONDS)) - 1
    return timedelta(seconds=COOLDOWN_STEPS_SECONDS[index])


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
            revision=row.risk_revision,
        )


async def open_user_validate(
    account_id: str, *, now: datetime | None = None, code: str | None = None
) -> WorkerRiskCircuit | None:
    """打开人工验证熔断,并停止该账号的 daemon 期望状态。

    code 用于记录实际命中的风控字面量;不传时按人工验证码记录,保持既有语义。
    """
    risk_code = code or USER_VALIDATE_CODE
    revision = uuid.uuid4().hex
    detected_at = (now or datetime.now(UTC)).astimezone(UTC)
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
        hits = int(row.risk_hit_count or 0) + 1
        cooldown = cooldown_for(hits)
        cooldown_until = detected_at + cooldown
        account.desired_state = WorkerDesiredState.STOPPED
        row.status = RISK_COOLING_STATUS
        row.last_error = risk_code
        row.risk_code = risk_code
        row.risk_revision = revision
        row.risk_detected_at = detected_at
        row.risk_cooldown_until = cooldown_until
        row.risk_recovery_required = True
        row.risk_hit_count = hits
        session.add(
            AuditLog(
                actor="system",
                action="ws_user_validate_circuit_opened",
                target=account_id,
                params={
                    "risk_code": risk_code,
                    "risk_revision": revision,
                    "cooldown_seconds": int(cooldown.total_seconds()),
                    "consecutive_hits": hits,
                },
                result="blocked",
                error=risk_code,
            )
        )
        await session.commit()
    return WorkerRiskCircuit(
        account_id=account_id,
        code=risk_code,
        detected_at=detected_at,
        cooldown_until=cooldown_until,
        recovery_required=True,
        revision=revision,
    )


async def clear_after_refresh(account_id: str, *, expected_revision: str | None = None) -> bool:
    """仅在一次人工触发的 Token 刷新成功后解除熔断。"""
    if expected_revision is None:
        return False
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
        statement = update(WorkerStatus).where(
            WorkerStatus.id == row.id, WorkerStatus.risk_recovery_required.is_(True)
        )
        if expected_revision is not None:
            statement = statement.where(WorkerStatus.risk_revision == expected_revision)
        result = await session.execute(
            statement.values(
                status=case((WorkerStatus.status == "online", "online"), else_="offline"),
                last_error=None,
                risk_code=None,
                risk_detected_at=None,
                risk_cooldown_until=None,
                risk_recovery_required=False,
                risk_hit_count=0,
            ).execution_options(synchronize_session=False)
        )
        if not result.rowcount:
            return False
        if previous_code == "TAKEOVER_FAILURE_LIMIT":
            account.takeover_failure_count = 0
            account.takeover_last_failure_at = None
            account.takeover_approved_at = None
            account.takeover_approved_by = None
        session.add(
            AuditLog(
                actor="system",
                action="ws_user_validate_circuit_cleared",
                target=account_id,
                params={
                    "risk_code": previous_code or USER_VALIDATE_CODE,
                    "risk_revision": row.risk_revision,
                },
                result="ok",
            )
        )
        await session.commit()
        return True


async def record_recovery_probe(
    account_id: str,
    *,
    result: str,
    reason: str | None = None,
) -> None:
    """Persist a secret-free live-session recovery probe outcome."""
    safe_reason = redact_text(reason)[:200] if reason else None
    async with get_async_session() as session:
        session.add(
            AuditLog(
                actor="system",
                action="ws_validation_recovery_probe",
                target=account_id,
                params={"result": result, **({"reason": safe_reason} if safe_reason else {})},
                result=result,
                error=safe_reason if result != "ok" else None,
            )
        )
        await session.commit()


async def start_block_reason(account_id: str) -> str | None:
    """返回启动阻断原因;解除前始终要求先执行成功的人工刷新。"""
    circuit = await get(account_id)
    if circuit is None:
        return None
    if circuit.is_cooling():
        return f"账号 {account_id} 处于 {circuit.code} 验证冷却;冷却结束后先执行 auth refresh。"
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
