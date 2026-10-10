"""OpenAI's own Chat Completions endpoint."""

from __future__ import annotations

from xianyu_agent.application.ai import ResolvedAIProvider

from .base import normalize_base_url
from .compatible import OpenAICompatibleProvider

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIChatProvider(OpenAICompatibleProvider):
    """Chat completions where the official endpoint is the default.

    This is the only provider type allowed to fall back to a built-in base URL;
    every other type requires the operator to state the endpoint explicitly.
    """

    name = "openai_chat"
    provider_type = "openai_chat"

    def endpoint(self, provider: ResolvedAIProvider) -> str:
        return f"{normalize_base_url(provider, DEFAULT_BASE_URL)}/chat/completions"
