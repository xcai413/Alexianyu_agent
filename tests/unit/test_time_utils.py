"""Unit tests for time display helpers (UTC -> local)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xianyu_agent.utils.time_utils import format_duration, format_local, to_local


def test_to_local_converts_utc() -> None:
    utc_dt = datetime(2026, 8, 10, 6, 27, 23, tzinfo=UTC)
    local = to_local(utc_dt)
    assert local is not None
    # 本地时间必须带非 UTC 偏移(测试机为 Asia/Shanghai +08:00)
    assert local.utcoffset() != timedelta(0)
    # 与 UTC 相差整小时(中国时区)
    assert (local.utcoffset() or timedelta(0)).total_seconds() % 3600 == 0


def test_to_local_none_passthrough() -> None:
    assert to_local(None) is None


def test_format_local_shape() -> None:
    s = format_local(datetime(2026, 8, 10, 6, 27, 23, tzinfo=UTC))
    assert s.startswith("2026-08-10T")
    assert s.endswith("+08:00")


def test_format_local_none_empty() -> None:
    assert format_local(None) == ""


def test_format_duration() -> None:
    assert format_duration(None) == "-"
    assert format_duration(65) == "00:01:05"
    assert format_duration(90_061) == "1d 01:01:01"


def test_to_local_handles_naive_utc_from_sqlite() -> None:
    """SQLite 经 SQLAlchemy 读出的 datetime 是 naive(值为 UTC),必须补 UTC 再转本地。"""
    naive_utc = datetime(2026, 8, 10, 6, 27, 23)  # 无 tzinfo,值上是 UTC
    local = to_local(naive_utc)
    assert local is not None
    assert local.hour == 14  # +08:00
    assert local.utcoffset() == timedelta(hours=8)


def test_format_local_naive_utc() -> None:
    assert format_local(datetime(2026, 8, 10, 6, 27, 23)) == "2026-08-10T14:27:23+08:00"
