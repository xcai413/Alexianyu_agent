"""In-process application command bus.

This bus deliberately does not replace ``worker_commands``: the latter remains
the durable cross-process Runtime control plane, while this module coordinates
commands inside one Application process.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

CommandHandler = Callable[[object], Awaitable[object]]


class HandlerNotRegisteredError(LookupError):
    """Raised when no handler exists for a command type."""


class DuplicateHandlerError(ValueError):
    """Raised when a command type already has a handler."""


class InProcessCommandBus:
    """Exact-type command dispatcher with one handler per command type."""

    def __init__(self) -> None:
        self._handlers: dict[type[object], CommandHandler] = {}

    def register(self, command_type: type[object], handler: CommandHandler) -> None:
        if command_type in self._handlers:
            raise DuplicateHandlerError(
                f"handler already registered for {command_type.__qualname__}"
            )
        self._handlers[command_type] = handler

    async def dispatch(self, command: object, /) -> object:
        command_type = type(command)
        try:
            handler = self._handlers[command_type]
        except KeyError as exc:
            raise HandlerNotRegisteredError(
                f"no handler registered for {command_type.__qualname__}"
            ) from exc
        return await handler(command)
