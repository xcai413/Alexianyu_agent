"""One AI generation request: the buyer message plus operator-approved context."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AIRequest:
    """Input for one model call.

    ``chat_context`` and ``item_facts`` are the only extra material a provider
    may use. Both are assembled by ContextService from bounded, local data, so a
    provider never reaches for the wider database or for buyer-authored
    instructions.
    """

    account_id: str
    buyer_message: str
    chat_context: Sequence[str] = field(default_factory=tuple)
    item_facts: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chat_context, tuple):
            object.__setattr__(self, "chat_context", tuple(self.chat_context))
