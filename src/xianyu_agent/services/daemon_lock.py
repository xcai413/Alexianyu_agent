"""跨平台 daemon 单实例锁。"""

from __future__ import annotations

import errno
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import IO


class DaemonAlreadyRunningError(RuntimeError):
    """锁已被另一个 daemon 进程持有。"""


class DaemonLock:
    """在 daemon 生命周期内持有非阻塞文件锁。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self, *, instance_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            self._lock(handle)
        except OSError as exc:
            handle.close()
            if self._is_lock_error(exc):
                raise DaemonAlreadyRunningError("daemon 单实例锁已被占用") from exc
            raise
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"instance_id": instance_id, "pid": os.getpid()}))
            handle.flush()
        except OSError:
            with suppress(OSError):
                self._unlock(handle)
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            with suppress(OSError):
                self._unlock(self._handle)
        finally:
            self._handle.close()
            self._handle = None

    @staticmethod
    def _is_lock_error(exc: OSError) -> bool:
        if os.name == "nt":
            return getattr(exc, "winerror", None) in {32, 33, 36} or exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
            }
        return exc.errno in {errno.EACCES, errno.EAGAIN}

    @staticmethod
    def _lock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            handle.seek(0)
            if handle.read(1) == "":
                handle.seek(0)
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl  # noqa: PLC0415

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl  # noqa: PLC0415

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> DaemonLock:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
