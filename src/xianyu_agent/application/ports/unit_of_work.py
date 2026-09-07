"""Unit-of-work contract owned by the application layer."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

from .outbox import OutboxRepository
from .repositories import AccountRepository


class UnitOfWork(Protocol):
    """Application transaction boundary.

    Repositories never commit on their own. Application use cases decide when a
    group of repository writes is durable by calling ``commit()`` on this port.
    """

    @property
    def accounts(self) -> AccountRepository: ...

    @property
    def outbox(self) -> OutboxRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
