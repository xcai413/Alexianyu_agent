"""Wire-format tests for the provider types declared by Issue #4 Phase 3."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from xianyu_agent.application.ai import ResolvedAIProvider
from xianyu_agent.application.ports.secret_vault import SecretValue
from xianyu_agent.domain.ai import AIRequest, UnconfiguredError
from xianyu_agent.infrastructure.ai.providers import PROVIDER_TYPES
from xianyu_agent.infrastructure.ai.providers.anthropic import AnthropicProvider
from xianyu_agent.infrastructure.ai.providers.azure_openai import AzureOpenAIProvider
from xianyu_agent.infrastructure.ai.providers.dashscope import DashScopeProvider
from xianyu_agent.infrastructure.ai.providers.gemini import GeminiProvider
from xianyu_agent.infrastructure.ai.providers.openai_responses import OpenAIResponsesProvider

REQUEST = AIRequest(account_id="a", buyer_message="在吗")
BASE_URL = "https://example.com/v1"


def _provider(
    provider_type: str, *, base_url: str = BASE_URL, extra_config: dict[str, Any] | None = None
) -> ResolvedAIProvider:
    return ResolvedAIProvider(
        provider_type=provider_type,
        base_url=base_url,
        default_model="test-model",
        timeout_seconds=15,
        max_retries=0,
        api_key=SecretValue("sk-test"),
        extra_config=extra_config or {},
    )


def _body(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls[0].request.content)


@pytest.mark.asyncio
async def test_openai_responses_provider_sends_input_and_reads_output_text() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(f"{BASE_URL}/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "output": [
                    {"type": "reasoning", "content": []},
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "在的,"},
                            {"type": "output_text", "text": "亲"},
                        ],
                    },
                ]
            },
        )
    )
    with router:
        response = await OpenAIResponsesProvider(_provider("openai_responses")).generate(REQUEST)
        sent = _body(route)
        assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"
    assert response.text == "在的,亲"
    assert sent["model"] == "test-model"
    assert sent["max_output_tokens"] == 200
    assert [message["role"] for message in sent["input"]] == ["system", "user"]


@pytest.mark.asyncio
async def test_anthropic_provider_uses_messages_api_shape() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "在的,亲"}]})
    )
    with router:
        response = await AnthropicProvider(_provider("anthropic")).generate(REQUEST)
        request = route.calls[0].request
        sent = json.loads(request.content)
        assert request.headers["x-api-key"] == "sk-test"
        assert request.headers["anthropic-version"] == "2023-06-01"
    assert response.text == "在的,亲"
    assert response.provider == "anthropic"
    assert sent["model"] == "test-model"
    assert "你是闲鱼卖家客服" in sent["system"]
    assert sent["messages"] == [{"role": "user", "content": "在吗"}]


@pytest.mark.asyncio
async def test_gemini_provider_uses_generate_content_shape() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1beta/models/test-model:generateContent").mock(
        return_value=httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "在的,亲"}]}}]}
        )
    )
    with router:
        response = await GeminiProvider(_provider("gemini")).generate(REQUEST)
        request = route.calls[0].request
        sent = json.loads(request.content)
        assert request.headers["x-goog-api-key"] == "sk-test"
    assert response.text == "在的,亲"
    assert sent["contents"] == [{"role": "user", "parts": [{"text": "在吗"}]}]
    assert "你是闲鱼卖家客服" in sent["systemInstruction"]["parts"][0]["text"]
    assert sent["generationConfig"]["maxOutputTokens"] == 200


@pytest.mark.asyncio
async def test_azure_provider_addresses_the_configured_deployment() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(
        "https://example.com/v1/openai/deployments/deploy-1/chat/completions?api-version=2024-06-01"
    ).mock(return_value=httpx.Response(200, json={"choices": [{"message": {"content": "在的"}}]}))
    provider = _provider("azure_openai", extra_config={"deployment": "deploy-1"})
    with router:
        response = await AzureOpenAIProvider(provider).generate(REQUEST)
        request = route.calls[0].request
        sent = json.loads(request.content)
        assert request.headers["api-key"] == "sk-test"
    assert response.text == "在的"
    assert "model" not in sent
    assert sent["messages"][-1] == {"role": "user", "content": "在吗"}


@pytest.mark.asyncio
async def test_dashscope_provider_uses_text_generation_shape() -> None:
    url = f"{BASE_URL}/services/aigc/text-generation/generation"
    router = respx.mock(assert_all_called=False)
    route = router.post(url).mock(
        return_value=httpx.Response(
            200, json={"output": {"choices": [{"message": {"content": "在的,亲"}}]}}
        )
    )
    with router:
        response = await DashScopeProvider(_provider("dashscope")).generate(REQUEST)
        sent = _body(route)
    assert response.text == "在的,亲"
    assert sent["model"] == "test-model"
    assert sent["parameters"]["result_format"] == "message"
    assert [message["role"] for message in sent["input"]["messages"]] == ["system", "user"]


@pytest.mark.asyncio
async def test_dashscope_provider_accepts_the_text_result_format() -> None:
    url = f"{BASE_URL}/services/aigc/text-generation/generation"
    router = respx.mock(assert_all_called=False)
    router.post(url).mock(return_value=httpx.Response(200, json={"output": {"text": "在的"}}))
    with router:
        response = await DashScopeProvider(_provider("dashscope")).generate(REQUEST)
    assert response.text == "在的"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_class", "provider_type", "payload"),
    [
        (OpenAIResponsesProvider, "openai_responses", {}),
        (AnthropicProvider, "anthropic", {}),
        (GeminiProvider, "gemini", {}),
        (DashScopeProvider, "dashscope", {}),
    ],
)
async def test_providers_report_malformed_responses(
    provider_class: type, provider_type: str, payload: dict[str, Any]
) -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__regex=r".*").mock(return_value=httpx.Response(200, json=payload))
    with router, pytest.raises(RuntimeError, match="格式异常"):
        await provider_class(_provider(provider_type)).generate(REQUEST)


@pytest.mark.asyncio
async def test_provider_without_an_endpoint_fails_closed() -> None:
    provider = _provider("azure_openai", base_url="")
    with pytest.raises(UnconfiguredError, match="base_url"):
        await AzureOpenAIProvider(provider).generate(REQUEST)


def test_registry_declares_every_phase_3_provider_type() -> None:
    assert set(PROVIDER_TYPES) == {
        "openai_chat",
        "openai_compatible",
        "openai_responses",
        "anthropic",
        "gemini",
        "azure_openai",
        "dashscope",
    }
