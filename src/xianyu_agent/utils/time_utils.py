"""时间显示工具:数据库统一存 UTC,展示时转本地时区。"""

from __future__ import annotations

from datetime import UTC, datetime


def to_local(dt: datetime | None) -> datetime | None:
    """数据库时间 -> 系统本地时区。

    SQLite 经 SQLAlchemy 读出的 datetime 是 naive(值为 UTC),astimezone()
    会把 naive 当作本地时间导致不转换;因此先补 UTC tzinfo 再转本地。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone()


def format_local(dt: datetime | None) -> str:
    """格式化为本地时间字符串(秒精度);None -> 空串。"""
    if dt is None:
        return ""
    return to_local(dt).isoformat(timespec="seconds")


def format_duration(seconds: float | None) -> str:
    """把秒数格式化为紧凑且稳定的运行时长。"""
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
