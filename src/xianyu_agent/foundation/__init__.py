"""Cross-entrypoint contracts shared by REST, MCP, CLI, TUI and automation."""

from .confirmation import ConfirmationChallenge
from .context import ActorContext, ActorSource, CommandContext
from .errors import ErrorCode, MachineError
from .identifiers import (
    CausationId,
    CorrelationId,
    IdempotencyKey,
    RequestId,
)
from .pagination import PageRequest, PageResult, SortDirection, SortSpec
from .time import ensure_utc, utc_now
from .versioning import API_PATH_PREFIX, API_VERSION

__all__ = [
    "API_PATH_PREFIX",
    "API_VERSION",
    "ActorContext",
    "ActorSource",
    "CausationId",
    "CommandContext",
    "ConfirmationChallenge",
    "CorrelationId",
    "ErrorCode",
    "IdempotencyKey",
    "MachineError",
    "PageRequest",
    "PageResult",
    "RequestId",
    "SortDirection",
    "SortSpec",
    "ensure_utc",
    "utc_now",
]
