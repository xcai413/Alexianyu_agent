"""Azure OpenAI chat completions, addressed by deployment rather than by model."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from xianyu_agent.application.ai import ResolvedAIProvider
from xianyu_agent.domain.ai import AIRequest

from .base import (
    MAX_OUTPUT_TOKENS,
    TEMPERATURE,
    build_chat_messages,
    normalize_base_url,
)
from .compatible import OpenAICompatibleProvider

DEFAULT_API_VERSION = "2024-06-01"


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Chat completions where the resource endpoint and deployment are explicit.

    The deployment name and API version are deployment configuration, so they
    come from extra_config (falling back to default_model for the deployment)
    instead of being derived from the model name.
    """

    name = "azure_openai"
    provider_type = "azure_openai"

    def deployment(self, provider: ResolvedAIProvider) -> str:
        deployment = str(provider.extra_config.get("deployment") or "").strip()
        return deployment or provider.default_model

    def api_version(self, provider: ResolvedAIProvider) -> str:
        return str(provider.extra_config.get("api_version") or DEFAULT_API_VERSION).strip()

    def endpoint(self, provider: ResolvedAIProvider) -> str:
        base_url = normalize_base_url(provider)
        deployment = quote(self.deployment(provider), safe="")
        api_version = quote(self.api_version(provider), safe="")
        return (
            f"{base_url}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
        )

    def headers(self, provider: ResolvedAIProvider) -> dict[str, str]:
        return {"api-key": provider.api_key.reveal(), "content-type": "application/json"}

    def payload(self, provider: ResolvedAIProvider, request: AIRequest) -> dict[str, Any]:
        # The deployment selects the model, so no model field is sent.
        _ = provider
        return {
            "messages": build_chat_messages(request, system_prompt=self._system_prompt),
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": TEMPERATURE,
        }
