"""AI reply providers: pluggable, no secrets in code.

The provider contract is intentionally minimal: given the buyer message and
optional chat context, return a reply string. Providers raise
UnconfiguredError when they cannot serve (missing key/model), and the
ReplyEngine treats that as "no AI reply" -> falls back to rules.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from xianyu_agent.config import get_settings

logger = logging.getLogger(__name__)


class UnconfiguredError(RuntimeError):
    """Raised when a provider is not configured (missing key/base_url/model)."""


class AIProvider(Protocol):
    name: str

    async def generate_reply(
        self,
        *,
        account_id: str,
        buyer_message: str,
        chat_context: list[str] | None = None,
    ) -> str: ...


class NullProvider:
    """Default provider: always unavailable. Keeps AI off until configured."""

    name = "null"

    async def generate_reply(
        self,
        *,
        account_id: str,
        buyer_message: str,
        chat_context: list[str] | None = None,
    ) -> str:
        logger.debug(
            "null provider asked for reply: account=%s msg=%s ctx=%s",
            account_id,
            buyer_message,
            len(chat_context or []),
        )
        msg = "AI 回复未配置(需 XIANYU_AI_BASE_URL / XIANYU_AI_API_KEY / XIANYU_AI_MODEL)"
        raise UnconfiguredError(msg)


class OpenAICompatibleProvider:
    """OpenAI-compatible chat completions via httpx.

    Reads base_url / api_key / model from settings at call time, so switching
    .env values takes effect without restarting the process.
    """

    name = "openai_compatible"

    def __init__(self, *, timeout_s: float = 15.0) -> None:
        self._timeout_s = timeout_s

    async def generate_reply(
        self,
        *,
        account_id: str,
        buyer_message: str,
        chat_context: list[str] | None = None,
    ) -> str:
        logger.debug("openai provider request: account=%s", account_id)
        s = get_settings()
        if not (s.ai_base_url and s.ai_api_key and s.ai_model):
            msg = "AI 回复未配置(需 XIANYU_AI_BASE_URL / XIANYU_AI_API_KEY / XIANYU_AI_MODEL)"
            raise UnconfiguredError(msg)
        url = f"{s.ai_base_url.rstrip('/')}/chat/completions"
        system = (
            "你是闲鱼卖家客服。回复买家要简短、口语化、像真人,不要暴露你"
            "是 AI,不要承诺平台规则外的事。"
        )
        messages = [{"role": "system", "content": system}]
        if chat_context:
            messages.extend({"role": "user", "content": line} for line in chat_context[-4:])
        messages.append({"role": "user", "content": buyer_message})
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {s.ai_api_key}"},
                json={
                    "model": s.ai_model,
                    "messages": messages,
                    "max_tokens": 200,
                    "temperature": 0.7,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            msg = f"AI 响应格式异常: {data}"
            raise RuntimeError(msg) from exc


def build_provider(name: str | None = None) -> AIProvider:
    """Factory: None/''/null -> NullProvider; 'openai'/'openai_compatible' -> configured provider."""
    n = (name or "null").lower()
    if n in {"openai", "openai_compatible", "compatible"}:
        return OpenAICompatibleProvider()
    return NullProvider()
