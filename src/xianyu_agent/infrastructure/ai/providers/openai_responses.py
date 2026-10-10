"""OpenAI's Responses API (POST /responses)."""

from __future__ import annotations

from typing import Any

from xianyu_agent.application.ai import ResolvedAIProvider
from xianyu_agent.domain.ai import AIRequest

from .base import (
    BAD_RESPONSE_MESSAGE,
    MAX_OUTPUT_TOKENS,
    HttpProvider,
    build_chat_messages,
    normalize_base_url,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIResponsesProvider(HttpProvider):
    """Responses API: input messages in, output_text parts out."""

    name = "openai_responses"
    provider_type = "openai_responses"

    def endpoint(self, provider: ResolvedAIProvider) -> str:
        return f"{normalize_base_url(provider, DEFAULT_BASE_URL)}/responses"

    def headers(self, provider: ResolvedAIProvider) -> dict[str, str]:
        return {"Authorization": f"Bearer {provider.api_key.reveal()}"}

    def payload(self, provider: ResolvedAIProvider, request: AIRequest) -> dict[str, Any]:
        return {
            "model": provider.default_model,
            "input": build_chat_messages(request),
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }

    def parse(self, data: dict[str, Any]) -> str:
        output = data.get("output")
        if not isinstance(output, list):
            raise RuntimeError(BAD_RESPONSE_MESSAGE)  # noqa: TRY004 - 远端响应格式错误
        texts = [
            str(part["text"])
            for item in output
            if isinstance(item, dict)
            for part in item.get("content") or []
            if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text")
        ]
        return "".join(texts).strip()
