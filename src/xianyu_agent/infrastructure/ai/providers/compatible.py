"""Provider for the OpenAI-compatible chat-completions wire format.

Many self-hosted and third-party gateways speak this format. The endpoint comes
from the configured base_url only; nothing is inferred from the model name or
from the URL shape, and a missing endpoint fails closed.
"""

from __future__ import annotations

from typing import Any

from xianyu_agent.application.ai import ResolvedAIProvider
from xianyu_agent.domain.ai import AIRequest

from .base import (
    BAD_RESPONSE_MESSAGE,
    CHAT_SYSTEM_PROMPT,
    MAX_OUTPUT_TOKENS,
    TEMPERATURE,
    HttpProvider,
    build_chat_messages,
    normalize_base_url,
)


class OpenAICompatibleProvider(HttpProvider):
    """Chat completions against an operator-supplied compatible endpoint."""

    name = "openai_compatible"
    provider_type = "openai_compatible"

    def __init__(
        self, provider: ResolvedAIProvider | None = None, *, system_prompt: str = CHAT_SYSTEM_PROMPT
    ) -> None:
        super().__init__(provider)
        self._system_prompt = system_prompt

    def endpoint(self, provider: ResolvedAIProvider) -> str:
        return f"{normalize_base_url(provider)}/chat/completions"

    def headers(self, provider: ResolvedAIProvider) -> dict[str, str]:
        return {"Authorization": f"Bearer {provider.api_key.reveal()}"}

    def payload(self, provider: ResolvedAIProvider, request: AIRequest) -> dict[str, Any]:
        return {
            "model": provider.default_model,
            "messages": build_chat_messages(request, system_prompt=self._system_prompt),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
        }

    def parse(self, data: dict[str, Any]) -> str:
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(BAD_RESPONSE_MESSAGE) from exc
