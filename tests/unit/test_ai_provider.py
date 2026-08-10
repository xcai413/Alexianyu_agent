"""Unit tests for AI providers (Null + OpenAI-compatible)."""

from __future__ import annotations

import httpx
import pytest
import respx

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.services.ai_provider import (
    NullProvider,
    OpenAICompatibleProvider,
    UnconfiguredError,
    build_provider,
)


@pytest.mark.asyncio
async def test_null_provider_raises_unconfigured() -> None:
    p = NullProvider()
    with pytest.raises(UnconfiguredError, match="未配置"):
        await p.generate_reply(account_id="a", buyer_message="在吗")


@pytest.mark.asyncio
async def test_openai_provider_unconfigured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XIANYU_AI_BASE_URL", raising=False)
    monkeypatch.delenv("XIANYU_AI_API_KEY", raising=False)
    monkeypatch.delenv("XIANYU_AI_MODEL", raising=False)
    reset_settings_cache()
    p = OpenAICompatibleProvider()
    with pytest.raises(UnconfiguredError, match="未配置"):
        await p.generate_reply(account_id="a", buyer_message="在吗")
    reset_settings_cache()


@pytest.mark.asyncio
async def test_openai_provider_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIANYU_AI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("XIANYU_AI_API_KEY", "sk-test")
    monkeypatch.setenv("XIANYU_AI_MODEL", "gpt-test")
    reset_settings_cache()
    router = respx.mock(assert_all_called=False)
    router.post("https://example.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "在的,亲"}}]})
    )
    with router:
        p = OpenAICompatibleProvider()
        reply = await p.generate_reply(account_id="a", buyer_message="在吗")
    assert reply == "在的,亲"
    reset_settings_cache()


@pytest.mark.asyncio
async def test_openai_provider_bad_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XIANYU_AI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("XIANYU_AI_API_KEY", "sk-test")
    monkeypatch.setenv("XIANYU_AI_MODEL", "gpt-test")
    reset_settings_cache()
    router = respx.mock(assert_all_called=False)
    router.post("https://example.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    with router:
        p = OpenAICompatibleProvider()
        with pytest.raises(RuntimeError, match="格式异常"):
            await p.generate_reply(account_id="a", buyer_message="在吗")
    reset_settings_cache()


def test_build_provider_factory() -> None:
    assert build_provider().name == "null"
    assert build_provider("").name == "null"
    assert build_provider("openai").name == "openai_compatible"
    assert build_provider("OpenAI_COMPATIBLE").name == "openai_compatible"
