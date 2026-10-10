"""One AI generation result."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AIResponse:
    """Model output plus the identifiers needed to explain where it came from.

    ``text`` is untrusted: it must pass the safety policy and the bargain policy
    before anything sends it to a buyer.
    """

    text: str
    provider: str | None = None
    model: str | None = None
