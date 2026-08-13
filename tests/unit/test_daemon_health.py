"""daemon 统一健康判定测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from xianyu_agent.services import daemon_health


def _row(*, status: str, heartbeat_age_s: float, pid: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        pid=pid,
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=heartbeat_age_s),
    )


def test_observe_daemon_online_requires_fresh_heartbeat_and_live_pid(monkeypatch) -> None:
    monkeypatch.setattr(daemon_health, "pid_alive", lambda _pid: True)
    health = daemon_health.observe_daemon(_row(status="running", heartbeat_age_s=10))
    assert health.healthy is True
    assert health.observed == "online"


def test_observe_daemon_marks_stale_heartbeat(monkeypatch) -> None:
    monkeypatch.setattr(daemon_health, "pid_alive", lambda _pid: True)
    health = daemon_health.observe_daemon(_row(status="running", heartbeat_age_s=120))
    assert health.healthy is False
    assert health.observed == "stale"


def test_observe_daemon_marks_dead_process(monkeypatch) -> None:
    monkeypatch.setattr(daemon_health, "pid_alive", lambda _pid: False)
    health = daemon_health.observe_daemon(_row(status="running", heartbeat_age_s=10))
    assert health.healthy is False
    assert health.observed == "dead"


def test_observe_daemon_keeps_stopped_record_historical(monkeypatch) -> None:
    monkeypatch.setattr(daemon_health, "pid_alive", lambda _pid: True)
    health = daemon_health.observe_daemon(_row(status="stopped", heartbeat_age_s=10))
    assert health.healthy is False
    assert health.process_alive is False
    assert health.observed == "stopped"
