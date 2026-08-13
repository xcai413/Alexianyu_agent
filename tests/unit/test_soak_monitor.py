"""P0-E 可恢复长稳监测测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from xianyu_agent.services import soak_monitor


def _params() -> dict[str, object]:
    return {
        "target_duration_s": 60.0,
        "interval_s": 10.0,
        "initial_daemon_instance": "d1",
        "sample_count": 0,
        "healthy_samples": 0,
        "error_samples": 0,
        "stale_samples": 0,
        "outage_count": 0,
        "outage_started_at": None,
        "max_outage_s": 0.0,
        "max_sample_gap_s": 0.0,
        "max_daemon_heartbeat_age_s": 0.0,
        "max_worker_heartbeat_age_s": 0.0,
        "max_reconnects": 0,
        "daemon_instance_changes": 0,
        "sensitive_log_hits": 0,
        "sensitive_log_files": [],
        "inbound_messages": 0,
        "outbound_messages": 0,
        "successful_replies": 0,
        "failed_replies": 0,
        "issues": [],
    }


def _sample(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "at": datetime.now(UTC).isoformat(),
        "daemon_instance": "d1",
        "daemon": "online",
        "daemon_heartbeat_age_s": 1.0,
        "actual": "connected",
        "worker_heartbeat_age_s": 2.0,
        "reconnect_attempts": 0,
        "healthy": True,
    }
    row.update(overrides)
    return row


def test_aggregate_tracks_health_and_instance_change() -> None:
    params = soak_monitor._aggregate(
        _params(),
        _sample(daemon_instance="d2", reconnect_attempts=2),
        metrics={
            "duplicate_reply_groups": 1,
            "inbound_messages": 2,
            "outbound_messages": 1,
            "successful_replies": 1,
            "failed_replies": 0,
        },
        unexpected_daemon_starts=1,
        sensitive_hits=3,
        sensitive_files=["daemon.log"],
        log_offsets={"daemon.log": 100},
    )
    assert params["sample_count"] == 1
    assert params["healthy_samples"] == 1
    assert params["daemon_instance_changes"] == 1
    assert params["max_reconnects"] == 2
    assert params["duplicate_reply_groups"] == 1
    assert params["inbound_messages"] == 2
    assert params["sensitive_log_hits"] == 3


def test_final_issues_passes_complete_healthy_run() -> None:
    started = datetime.now(UTC) - timedelta(seconds=61)
    params = _params()
    params.update(
        sample_count=6,
        healthy_samples=6,
        error_samples=0,
        max_sample_gap_s=10.5,
        unexpected_daemon_starts=0,
        duplicate_reply_groups=0,
        sensitive_log_hits=0,
    )
    assert soak_monitor._final_issues(params, started_at=started, now=datetime.now(UTC)) == []


def test_final_issues_reports_each_failed_gate() -> None:
    now = datetime.now(UTC)
    params = _params()
    params.update(
        sample_count=1,
        error_samples=1,
        max_sample_gap_s=30.0,
        daemon_instance_changes=1,
        unexpected_daemon_starts=1,
        duplicate_reply_groups=1,
        sensitive_log_hits=1,
        monitor_errors=1,
    )
    issues = soak_monitor._final_issues(params, started_at=now, now=now)
    assert "实际时长不足" in issues
    assert "采样覆盖率不足 90%" in issues
    assert "采样中断超过阈值" in issues
    assert "存在 daemon/账号异常样本" in issues
    assert "监测器自身发生采样错误" in issues
    assert "daemon 实例发生变化" in issues
    assert "检测到重复消息回复" in issues
    assert "日志检测到敏感值" in issues


def test_build_sample_never_includes_plain_credentials() -> None:
    source = soak_monitor._build_sample
    assert "cookie" not in source.__code__.co_varnames
    assert "token" not in source.__code__.co_varnames


def test_aggregate_tracks_and_closes_outage() -> None:
    first_at = datetime.now(UTC)
    params = soak_monitor._aggregate(
        _params(),
        _sample(at=first_at.isoformat(), healthy=False, actual="reconnecting"),
        metrics={
            "duplicate_reply_groups": 0,
            "inbound_messages": 0,
            "outbound_messages": 0,
            "successful_replies": 0,
            "failed_replies": 0,
        },
        unexpected_daemon_starts=0,
        sensitive_hits=0,
        sensitive_files=[],
        log_offsets={},
    )
    assert params["outage_count"] == 1
    params = soak_monitor._aggregate(
        params,
        _sample(at=(first_at + timedelta(seconds=20)).isoformat()),
        metrics={
            "duplicate_reply_groups": 0,
            "inbound_messages": 0,
            "outbound_messages": 0,
            "successful_replies": 0,
            "failed_replies": 0,
        },
        unexpected_daemon_starts=0,
        sensitive_hits=0,
        sensitive_files=[],
        log_offsets={},
    )
    assert params["outage_started_at"] is None
    assert params["max_outage_s"] == 20.0


@pytest.mark.asyncio
async def test_log_scan_is_incremental_and_handles_rotation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "daemon.log"
    path.write_text("safe\n", encoding="utf-8")

    async def secrets() -> set[str]:
        return {"secret-value"}

    monkeypatch.setattr(
        soak_monitor,
        "get_settings",
        lambda: SimpleNamespace(log_dir=tmp_path),
    )
    monkeypatch.setattr(soak_monitor, "_sensitive_values", secrets)
    hits, files, offsets = await soak_monitor._scan_log_changes({})
    assert hits == 0
    assert files == []

    with path.open("a", encoding="utf-8") as output:
        output.write("secret-value\n")
    hits, files, offsets = await soak_monitor._scan_log_changes(offsets)
    assert hits == 1
    assert files == ["daemon.log"]
    hits, files, offsets = await soak_monitor._scan_log_changes(offsets)
    assert hits == 0

    path.write_text("secret-value\n", encoding="utf-8")
    hits, files, _offsets = await soak_monitor._scan_log_changes(offsets)
    assert hits == 1
    assert files == ["daemon.log"]
