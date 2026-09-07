"""Per-account cross-process lock preventing duplicate live WS sessions."""

from __future__ import annotations

from pathlib import Path

from .daemon_lock import DaemonAlreadyRunningError, DaemonLock


class AccountConnectionAlreadyRunningError(RuntimeError):
    """Another process already owns this account's live connection."""


class AccountConnectionLock:
    def __init__(self, path: Path) -> None:
        self._lock = DaemonLock(path)

    def acquire(self, *, owner_id: str) -> None:
        try:
            self._lock.acquire(instance_id=owner_id)
        except DaemonAlreadyRunningError as exc:
            raise AccountConnectionAlreadyRunningError("账号 WS 连接锁已被占用") from exc

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> AccountConnectionLock:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
