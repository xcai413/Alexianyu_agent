"""Application command dispatch contract.

Commands request an action and stay distinct from durable domain events and the
SQLite worker control plane used for cross-process runtime coordination.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

CommandT = TypeVar("CommandT")
ResultT = TypeVar("ResultT", covariant=True)


class CommandHandler(Protocol[CommandT, ResultT]):
    """Handle one application command."""

    async def __call__(self, command: CommandT, /) -> ResultT: ...


class CommandBus(Protocol):
    """Dispatch application commands to their registered handler."""

    async def dispatch(self, command: object, /) -> object: ...
