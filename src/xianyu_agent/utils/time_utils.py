"""时间显示工具:数据库统一存 UTC,展示时转业务时区。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

# 当前个人闲鱼运营默认业务时区。不要依赖宿主机/CI Runner 的系统时区,
# 否则同一 UTC 时间在不同部署环境会展示为不同结果。
DEFAULT_BUSINESS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def to_local(dt: datetime | None) -> datetime | None:
    """数据库时间 -> 默认业务时区。

    SQLite 经 SQLAlchemy 读出的 datetime 是 naive(值为 UTC)。先补 UTC tzinfo,
    再显式转换到业务时区,避免 astimezone() 隐式依赖宿主机本地时区。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(DEFAULT_BUSINESS_TIMEZONE)


def format_local(dt: datetime | None) -> str:
    """格式化为业务时区时间字符串(秒精度);None -> 空串。"""
    if dt is None:
        return ""
    local = to_local(dt)
    assert local is not None
    return local.isoformat(timespec="seconds")


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
