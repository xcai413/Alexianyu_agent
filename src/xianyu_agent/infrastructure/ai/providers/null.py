"""The default provider: always unavailable, so AI stays off until configured."""

from __future__ import annotations

import logging

from xianyu_agent.domain.ai import AIRequest, AIResponse, UnconfiguredError

logger = logging.getLogger(__name__)


class NullProvider:
    """Selected when no provider type is configured."""

    name = "null"
    provider_type = "null"

    async def generate(self, request: AIRequest) -> AIResponse:
        logger.debug(
            "null provider asked for a reply: account=%s context_messages=%d",
            request.account_id,
            len(request.chat_context),
        )
        msg = "AI 回复未配置(需已启用的 Provider 与密钥)"
        raise UnconfiguredError(msg)
