"""daemon 与账号 Worker 的统一只读运行快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from xianyu_agent.application.health.runtime_health import DaemonHealth, observe_daemon
from xianyu_agent.db import DaemonInstance
from xianyu_agent.domain.account import state as domain_accounts
from xianyu_agent.domain.runtime import daemon as daemon_domain
from xianyu_agent.utils.time_utils import format_duration

WORKER_STALE_AFTER_S = 90.0
ACTIVE_WORKER_STATES = {"connected", "connecting", "reconnecting"}


@dataclass(frozen=True)
class AccountObservation:
    account_id: str
    enabled: bool
    desired_state: str
    actual_state: str
    aligned: bool
    heartbeat_age_s: float | None
    reconnect_attempts: int
    last_heartbeat_at: datetime | None
    last_error: str | None
    risk_code: str | None
    risk_cooldown_until: datetime | None
    risk_status: str | None
    issue: str | None


@dataclass(frozen=True)
class RuntimeSnapshot:
    daemon: DaemonInstance | None
    daemon_health: DaemonHealth
    daemon_uptime_s: float | None
    accounts: tuple[AccountObservation, ...]

    @property
    def drifted_accounts(self) -> tuple[AccountObservation, ...]:
        return tuple(account for account in self.accounts if not account.aligned)

    @property
    def operational_alerts(self) -> tuple[str, ...]:
        alerts: list[str] = []
        expects_workers = any(
            account.enabled and account.desired_state == "running" for account in self.accounts
        )
        if not self.daemon_health.healthy and expects_workers:
            alerts.append(f"daemon {self.daemon_health.observed}")
        alerts.extend(
            f"{account.account_id}: {account.issue}"
            for account in self.drifted_accounts
            if account.issue
        )
        alerts.extend(
            f"{account.account_id}: {account.risk_status}"
            for account in self.accounts
            if account.risk_status
        )
        return tuple(alerts)


async def build_runtime_snapshot(*, now: datetime | None = None) -> RuntimeSnapshot:
    """从 SQLite 与 PID 读取跨进程一致的 daemon/Worker 快照。"""
    current = now or datetime.now(UTC)
    daemon = await daemon_domain.latest_instance()
    daemon_health = observe_daemon(daemon, now=current)
    uptime_s = None
    if daemon is not None:
        ended_at = daemon.stopped_at if daemon.status not in daemon_domain.ACTIVE_STATUSES else None
        uptime_s = _elapsed_seconds(daemon.started_at, ended_at or current)

    accounts = await domain_accounts.list_accounts()
    status_rows = await domain_accounts.worker_statuses()
    by_account_id = {row.account_id: row for row in status_rows}
    observations: list[AccountObservation] = []
    for account in accounts:
        row = by_account_id.get(account.id)
        actual = str(row.status) if row is not None else "offline"
        heartbeat = row.last_heartbeat_at if row is not None else None
        heartbeat_age_s = _age_seconds(heartbeat, current) if heartbeat else None
        risk_code = row.risk_code if row is not None else None
        risk_cooldown_until = row.risk_cooldown_until if row is not None else None
        risk_status = _risk_status(
            code=risk_code,
            cooldown_until=risk_cooldown_until,
            recovery_required=bool(row.risk_recovery_required) if row is not None else False,
            now=current,
        )
        if (
            actual == "connected"
            and heartbeat_age_s is not None
            and heartbeat_age_s > WORKER_STALE_AFTER_S
        ):
            actual = "stale"
        aligned, issue = _alignment(
            enabled=account.enabled,
            desired_state=account.desired_state,
            actual_state=actual,
        )
        observations.append(
            AccountObservation(
                account_id=account.account_id,
                enabled=account.enabled,
                desired_state=account.desired_state,
                actual_state=actual,
                aligned=aligned,
                heartbeat_age_s=heartbeat_age_s,
                reconnect_attempts=row.reconnect_attempts if row is not None else 0,
                last_heartbeat_at=heartbeat,
                last_error=row.last_error if row is not None else None,
                risk_code=risk_code,
                risk_cooldown_until=risk_cooldown_until,
                risk_status=risk_status,
                issue=issue,
            )
        )
    return RuntimeSnapshot(
        daemon=daemon,
        daemon_health=daemon_health,
        daemon_uptime_s=uptime_s,
        accounts=tuple(observations),
    )


def _alignment(*, enabled: bool, desired_state: str, actual_state: str) -> tuple[bool, str | None]:
    if not enabled and desired_state != "stopped":
        return False, "停用账号的期望状态不是 stopped"
    expects_running = enabled and desired_state == "running"
    if expects_running:
        if actual_state == "connected":
            return True, None
        if actual_state == "stale":
            return False, "Worker 心跳过期"
        return False, f"期望 running,实际 {actual_state}"
    if actual_state in ACTIVE_WORKER_STATES or actual_state == "stale":
        return False, f"期望 stopped,实际 {actual_state}"
    return True, None


def _age_seconds(value: datetime, now: datetime) -> float:
    timestamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return max(0.0, (now.astimezone(UTC) - timestamp).total_seconds())


def _elapsed_seconds(start: datetime, end: datetime) -> float:
    started_at = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
    ended_at = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
    return max(0.0, (ended_at - started_at).total_seconds())


def _risk_status(
    *,
    code: str | None,
    cooldown_until: datetime | None,
    recovery_required: bool,
    now: datetime,
) -> str | None:
    if not code or not recovery_required:
        return None
    if cooldown_until is None:
        return f"{code},需手动 auth refresh"
    remaining = _elapsed_seconds(now, cooldown_until)
    if remaining > 0:
        return f"{code} 验证冷却 {format_duration(remaining)}"
    return f"{code} 冷却结束,需手动 auth refresh"
