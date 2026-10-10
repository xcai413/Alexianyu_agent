"""Google Gemini generateContent (POST /v1beta/models/{model}:generateContent)."""

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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiProvider(HttpProvider):
    """Gemini generateContent, keyed through a header rather than a URL parameter."""

    name = "gemini"
    provider_type = "gemini"

    def endpoint(self, provider: ResolvedAIProvider) -> str:
        base_url = normalize_base_url(provider, DEFAULT_BASE_URL)
        return f"{base_url}/v1beta/models/{provider.default_model}:generateContent"

    def headers(self, provider: ResolvedAIProvider) -> dict[str, str]:
        return {"x-goog-api-key": provider.api_key.reveal(), "content-type": "application/json"}

    def payload(self, provider: ResolvedAIProvider, request: AIRequest) -> dict[str, Any]:
        _ = provider  # 模型名出现在 URL 路径中,正文不需要重复
        return {
            "systemInstruction": {"parts": [{"text": build_system_prompt(request)}]},
            "contents": [{"role": "user", "parts": [{"text": request.buyer_message}]}],
            "generationConfig": {
                "maxOutputTokens": MAX_OUTPUT_TOKENS,
                "temperature": TEMPERATURE,
            },
        }

    def parse(self, data: dict[str, Any]) -> str:
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            raise RuntimeError(BAD_RESPONSE_MESSAGE)  # noqa: TRY004 - 远端响应格式错误
        texts = [
            str(part["text"])
            for candidate in candidates
            for part in _text_parts(candidate)
            if part.get("text")
        ]
        return "".join(texts).strip()


def _text_parts(candidate: Any) -> list[dict[str, Any]]:
    """Return the text parts of one candidate, ignoring anything else."""
    if not isinstance(candidate, dict):
        return []
    content = candidate.get("content")
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    if not isinstance(parts, list):
        return []
    return [part for part in parts if isinstance(part, dict)]
