"""Provider registry: the configured provider_type selects the implementation.

Nothing is inferred from a model name or from the shape of a base URL. An
unknown type fails closed instead of falling back to a guess.
"""

from __future__ import annotations

from xianyu_agent.application.ai import ResolvedAIProvider
from xianyu_agent.domain.ai import (
    PROVIDER_TYPES as CANONICAL_PROVIDER_TYPES,
    AIProvider,
    UnconfiguredError,
)

from .anthropic import AnthropicProvider
from .azure_openai import AzureOpenAIProvider
from .compatible import OpenAICompatibleProvider
from .dashscope import DashScopeProvider
from .gemini import GeminiProvider
from .null import NullProvider
from .openai_chat import OpenAIChatProvider
from .openai_responses import OpenAIResponsesProvider

PROVIDER_TYPES: dict[str, type] = {
    "openai_chat": OpenAIChatProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "openai_responses": OpenAIResponsesProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "azure_openai": AzureOpenAIProvider,
    "dashscope": DashScopeProvider,
}
NULL_PROVIDER_TYPE = "null"

if set(PROVIDER_TYPES) != set(CANONICAL_PROVIDER_TYPES):  # pragma: no cover - import guard
    msg = "Provider 注册表与 domain 声明的类型不一致"
    raise RuntimeError(msg)


def build_provider(
    provider_type: str | None, provider: ResolvedAIProvider | None = None
) -> AIProvider:
    """Build the provider named by configuration, or fail closed."""
    kind = (provider_type or "").strip().lower()
    if not kind or kind == NULL_PROVIDER_TYPE:
        return NullProvider()
    implementation = PROVIDER_TYPES.get(kind)
    if implementation is None:
        msg = f"未知 AI Provider 类型: {provider_type}"
        raise UnconfiguredError(msg)
    return implementation(provider)  # type: ignore[no-any-return]
