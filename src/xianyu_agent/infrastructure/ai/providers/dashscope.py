"""Alibaba DashScope text generation (POST /services/aigc/text-generation/generation)."""

from __future__ import annotations

from typing import Any

from xianyu_agent.application.ai import ResolvedAIProvider
from xianyu_agent.domain.ai import AIRequest

from .base import (
    BAD_RESPONSE_MESSAGE,
    MAX_OUTPUT_TOKENS,
    TEMPERATURE,
    HttpProvider,
    build_chat_messages,
    normalize_base_url,
)

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
MESSAGE_RESULT_FORMAT = "message"


class DashScopeProvider(HttpProvider):
    """DashScope native text generation, asked for the message result format."""

    name = "dashscope"
    provider_type = "dashscope"

    def endpoint(self, provider: ResolvedAIProvider) -> str:
        base_url = normalize_base_url(provider, DEFAULT_BASE_URL)
        return f"{base_url}/services/aigc/text-generation/generation"

    def headers(self, provider: ResolvedAIProvider) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {provider.api_key.reveal()}",
            "content-type": "application/json",
        }

    def payload(self, provider: ResolvedAIProvider, request: AIRequest) -> dict[str, Any]:
        return {
            "model": provider.default_model,
            "input": {"messages": build_chat_messages(request)},
            "parameters": {
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": TEMPERATURE,
                "result_format": MESSAGE_RESULT_FORMAT,
            },
        }

    def parse(self, data: dict[str, Any]) -> str:
        output = data.get("output")
        if not isinstance(output, dict):
            raise RuntimeError(BAD_RESPONSE_MESSAGE)  # noqa: TRY004 - 远端响应格式错误
        choices = output.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and message.get("content") is not None:
                    return str(message["content"]).strip()
        if output.get("text") is not None:
            return str(output["text"]).strip()
        return ""
