"""Bounded conversation material handed to an AI provider."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AIConversationContext:
    """Non-sensitive facts and bounded message history for one AI request.

    ``messages`` excludes the inbound message being answered, so the current
    buyer turn never appears both as history and as the prompt.
    """

    messages: tuple[str, ...]
    item_facts: str | None
