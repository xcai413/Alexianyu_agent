"""Per-account cross-process lock preventing duplicate live WS sessions."""

from __future__ import annotations

from pathlib import Path

from .daemon_lock import DaemonAlreadyRunningError, DaemonLock


class AccountConnectionAlreadyRunningError(RuntimeError):
    """Another process already owns this account's live connection."""


class AccountConnectionLock:
    def __init__(self, path: Path) -> None:
        self._lock = DaemonLock(path)
        self._transport_handoff_pending = False

    def acquire(self, *, owner_id: str) -> None:
        if self._transport_handoff_pending:
            self._transport_handoff_pending = False
            return
        try:
            self._lock.acquire(instance_id=owner_id)
        except DaemonAlreadyRunningError as exc:
            raise AccountConnectionAlreadyRunningError("账号 WS 连接锁已被占用") from exc

    def acquire_for_transport_handoff(self, *, owner_id: str) -> None:
        """Hold the canonical lock across credential preparation and ``WsClient.start``.

        The next ``acquire`` on this same lock instance transfers ownership to the
        transport without releasing the underlying cross-process lock in between.
        """
        if self._transport_handoff_pending:
            msg = "账号 WS 连接锁已有待移交的 transport owner"
            raise RuntimeError(msg)
        try:
            self._lock.acquire(instance_id=owner_id)
        except DaemonAlreadyRunningError as exc:
            raise AccountConnectionAlreadyRunningError("账号 WS 连接锁已被占用") from exc
        self._transport_handoff_pending = True

    def release(self) -> None:
        self._transport_handoff_pending = False
        self._lock.release()

    def __enter__(self) -> AccountConnectionLock:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
