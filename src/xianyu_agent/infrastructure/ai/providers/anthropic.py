"""Anthropic Messages API (POST /v1/messages)."""

from __future__ import annotations

from typing import Any

from xianyu_agent.application.ai import ResolvedAIProvider
from xianyu_agent.domain.ai import AIRequest

from .base import (
    BAD_RESPONSE_MESSAGE,
    MAX_OUTPUT_TOKENS,
    TEMPERATURE,
    HttpProvider,
    build_system_prompt,
    normalize_base_url,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(HttpProvider):
    """Anthropic Messages: one system string plus user turns."""

    name = "anthropic"
    provider_type = "anthropic"

    def endpoint(self, provider: ResolvedAIProvider) -> str:
        return f"{normalize_base_url(provider, DEFAULT_BASE_URL)}/v1/messages"

    def headers(self, provider: ResolvedAIProvider) -> dict[str, str]:
        return {
            "x-api-key": provider.api_key.reveal(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def payload(self, provider: ResolvedAIProvider, request: AIRequest) -> dict[str, Any]:
        return {
            "model": provider.default_model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
            "system": build_system_prompt(request),
            "messages": [{"role": "user", "content": request.buyer_message}],
        }

    def parse(self, data: dict[str, Any]) -> str:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise RuntimeError(BAD_RESPONSE_MESSAGE)  # noqa: TRY004 - 远端响应格式错误
        texts = [
            str(block["text"])
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        return "".join(texts).strip()
