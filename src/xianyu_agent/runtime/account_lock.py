"""Per-account cross-process lock preventing duplicate live WS sessions."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from .daemon_lock import DaemonAlreadyRunningError, DaemonLock


class AccountConnectionAlreadyRunningError(RuntimeError):
    """Another process already owns this account's live connection."""


class AccountConnectionLock:
    _quarantined: ClassVar[set[AccountConnectionLock]] = set()

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
        if self in self._quarantined:
            return
        self._transport_handoff_pending = False
        self._lock.release()

    def quarantine(self) -> None:
        """Retain the OS lock until process exit when transport closure is unproven."""
        if self._lock._handle is not None:
            self._quarantined.add(self)

    def borrow(self) -> AccountConnectionLock:
        """Borrow a held lock while its caller retains ownership of release."""
        if self._lock._handle is None:
            raise RuntimeError("cannot borrow an unheld account lock")
        return _BorrowedAccountConnectionLock(self)

    def __enter__(self) -> AccountConnectionLock:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class _BorrowedAccountConnectionLock(AccountConnectionLock):
    def __init__(self, owner: AccountConnectionLock) -> None:
        self._owner = owner

    def acquire(self, *, owner_id: str) -> None:
        if not owner_id:
            raise ValueError("borrowed account lock requires an owner id")
        if self._owner._lock._handle is None:
            raise RuntimeError("borrowed account lock owner has released ownership")

    def release(self) -> None:
        """The outer owner releases only after transport cleanup."""

    def quarantine(self) -> None:
        self._owner.quarantine()
