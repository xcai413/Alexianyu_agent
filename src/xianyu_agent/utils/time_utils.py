"""时间显示工具:数据库统一存 UTC,展示时转本地时区。"""

from __future__ import annotations

from datetime import datetime


def to_local(dt: datetime | None) -> datetime | None:
    """UTC -> 系统本地时区(None 保持 None)。"""
    return dt.astimezone() if dt is not None else None


def format_local(dt: datetime | None) -> str:
    """格式化为本地时间字符串(秒精度);None -> 空串。"""
    if dt is None:
        return ""
    return dt.astimezone().isoformat(timespec="seconds")
