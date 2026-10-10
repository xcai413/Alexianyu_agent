"""The AI provider port: one explicit protocol every provider implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .request import AIRequest
from .response import AIResponse

PROVIDER_TYPES: tuple[str, ...] = (
    "openai_chat",
    "openai_compatible",
    "openai_responses",
    "anthropic",
    "gemini",
    "azure_openai",
    "dashscope",
)


class UnconfiguredError(RuntimeError):
    """Raised when a provider cannot serve because it is not configured.

    Callers treat this as "no AI reply available" and fall back to the configured
    reply pipeline instead of surfacing a failure to the buyer.
    """


@runtime_checkable
class AIProvider(Protocol):
    """Generate one reply for one request.

    ``name`` and ``provider_type`` are explicit configuration, never inferred
    from a model name or a base URL.
    """

    name: str
    provider_type: str

    async def generate(self, request: AIRequest) -> AIResponse: ...
