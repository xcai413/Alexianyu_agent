"""统一判断 daemon 进程与持久化心跳是否健康。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from xianyu_agent.domain import daemon as daemon_domain
from xianyu_agent.utils.process_utils import pid_alive

STALE_AFTER_S = 90.0


class DaemonRow(Protocol):
    status: str
    pid: int
    last_heartbeat_at: datetime


@dataclass(frozen=True)
class DaemonHealth:
    observed: str
    active: bool
    process_alive: bool
    heartbeat_age_s: float | None
    healthy: bool


def observe_daemon(
    row: DaemonRow | None,
    *,
    now: datetime | None = None,
    stale_after_s: float = STALE_AFTER_S,
) -> DaemonHealth:
    """合并数据库状态、PID 和心跳,给所有接口提供同一事实。"""
    if row is None:
        return DaemonHealth(
            observed="none",
            active=False,
            process_alive=False,
            heartbeat_age_s=None,
            healthy=False,
        )

    heartbeat = row.last_heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    age_s = max(
        0.0,
        (current.astimezone(UTC) - heartbeat.astimezone(UTC)).total_seconds(),
    )
    active = row.status in daemon_domain.ACTIVE_STATUSES
    process_alive = active and pid_alive(row.pid)
    healthy = row.status == "running" and process_alive and age_s <= stale_after_s

    if healthy:
        observed = "online"
    elif active and not process_alive:
        observed = "dead"
    elif active and age_s > stale_after_s:
        observed = "stale"
    else:
        observed = row.status

    return DaemonHealth(
        observed=observed,
        active=active,
        process_alive=process_alive,
        heartbeat_age_s=age_s,
        healthy=healthy,
    )
