"""Compatibility imports for canonical runtime heartbeat maintenance."""

from xianyu_agent.runtime.heartbeat import DEFAULT_RETENTION_HOURS, purge_old_messages

__all__ = ["DEFAULT_RETENTION_HOURS", "purge_old_messages"]
