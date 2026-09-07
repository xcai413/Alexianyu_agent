"""Actor and command context propagated across all entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .identifiers import CausationId, CorrelationId, IdempotencyKey, RequestId


class ActorSource(StrEnum):
    WEB = "web"
    CLI = "cli"
    MCP = "mcp"
    TUI = "tui"
    AUTOMATION = "automation"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Who initiated work and through which stable system surface."""

    source: ActorSource
    actor_id: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.actor_id is not None:
            actor_id = self.actor_id.strip()
            if not actor_id:
                raise ValueError("actor_id must not be blank")
            object.__setattr__(self, "actor_id", actor_id)
        normalized_roles = frozenset(role.strip() for role in self.roles if role.strip())
        object.__setattr__(self, "roles", normalized_roles)


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Transport-neutral context required by application commands."""

    actor: ActorContext
    request_id: RequestId
    correlation_id: CorrelationId
    causation_id: CausationId | None = None
    idempotency_key: IdempotencyKey | None = None
    timezone_name: str = "UTC"

    def __post_init__(self) -> None:
        timezone_name = self.timezone_name.strip()
        if not timezone_name:
            raise ValueError("timezone_name must not be blank")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {timezone_name}") from exc
        object.__setattr__(self, "timezone_name", timezone_name)

    @classmethod
    def create(
        cls,
        actor: ActorContext,
        *,
        request_id: RequestId | None = None,
        correlation_id: CorrelationId | None = None,
        causation_id: CausationId | None = None,
        idempotency_key: IdempotencyKey | None = None,
        timezone_name: str = "UTC",
    ) -> CommandContext:
        return cls(
            actor=actor,
            request_id=request_id or RequestId.new(),
            correlation_id=correlation_id or CorrelationId.new(),
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            timezone_name=timezone_name,
        )
