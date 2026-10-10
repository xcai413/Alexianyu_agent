"""外部发卡 API 调用与结果分类(Issue #5 Phase 6, §18/M20/M22)。"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest

from xianyu_agent.application.ports.secret_vault import SecretRef, SecretValue
from xianyu_agent.domain.delivery import ApiDeliveryConfig
from xianyu_agent.infrastructure.delivery import ApiDeliveryProvider

PUBLIC_URL = "https://cards.example.com/dispatch"


class _Vault:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    async def resolve(self, ref: SecretRef) -> SecretValue:
        if ref.value not in self._values:
            raise KeyError(ref.value)
        return SecretValue(self._values[ref.value])


def _config(**kwargs: object) -> ApiDeliveryConfig:
    payload: dict[str, object] = {"name": "发卡接口", "url": PUBLIC_URL}
    payload.update(kwargs)
    return ApiDeliveryConfig(**payload)  # type: ignore[arg-type]


def _provider(handler, **kwargs: object) -> ApiDeliveryProvider:
    return ApiDeliveryProvider(transport=httpx.MockTransport(handler), resolve_dns=False, **kwargs)


async def test_success_returns_card_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json={"data": {"card": " CODE-1 "}})

    result = await _provider(handler).fetch(_config(response_path="data.card"), {})

    assert result.ok is True
    assert result.content == "CODE-1"
    assert result.dispatched is True
    assert result.status_code == 200


async def test_missing_content_is_uncertain() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    result = await _provider(handler).fetch(_config(response_path="data.card"), {})

    assert result.content is None
    assert result.dispatched is True
    assert "没有可发送的内容" in (result.error or "")


async def test_non_json_response_is_uncertain() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>ok</html>")

    result = await _provider(handler).fetch(_config(), {})

    assert result.dispatched is True
    assert "不是合法 JSON" in (result.error or "")


async def test_client_error_is_rejected_without_side_effect() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    result = await _provider(handler).fetch(_config(), {})

    assert result.dispatched is False
    assert result.status_code == 403
    assert "未发卡" in (result.error or "")


async def test_server_error_is_uncertain() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    result = await _provider(handler).fetch(_config(), {})

    assert result.dispatched is True
    assert result.status_code == 503
    assert "可能已经发卡" in (result.error or "")


async def test_connect_failure_is_not_dispatched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    result = await _provider(handler).fetch(_config(), {})

    assert result.dispatched is False
    assert "建连失败" in (result.error or "")


async def test_read_timeout_is_uncertain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = await _provider(handler).fetch(_config(), {})

    assert result.dispatched is True
    assert "结果不明" in (result.error or "")


async def test_outbound_guard_blocks_unsafe_targets() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        return httpx.Response(200, json={"card": "X"})

    for url in ("https://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/x"):
        result = await _provider(handler).fetch(_config(url=url), {})
        assert result.dispatched is False
        assert "出站被拦截" in (result.error or "")


async def test_redirect_target_is_revalidated() -> None:
    def blocked(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://127.0.0.1/steal"})

    def allowed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://other.example.com/card"})

    to_blocked = await _provider(blocked).fetch(_config(), {})
    to_allowed = await _provider(allowed).fetch(_config(), {})

    assert to_blocked.dispatched is False
    assert "重定向目标被拦截" in (to_blocked.error or "")
    assert to_allowed.dispatched is False
    assert "等待人工确认" in (to_allowed.error or "")


async def test_secret_is_sent_but_never_appears_in_errors() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(500)

    config = _config(headers={"Authorization": "Bearer {{secret:key}}"})
    provider = ApiDeliveryProvider(
        vault=_Vault({"key": "SUPER-SECRET"}),
        transport=httpx.MockTransport(handler),
        resolve_dns=False,
    )

    result = await provider.fetch(config, {})

    assert seen["authorization"] == "Bearer SUPER-SECRET"
    assert "SUPER-SECRET" not in (result.error or "")
    assert result.dispatched is True


async def test_missing_secret_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        return httpx.Response(200, json={"card": "X"})

    config = _config(headers={"Authorization": "Bearer {{secret:key}}"})
    provider = ApiDeliveryProvider(
        vault=_Vault({}), transport=httpx.MockTransport(handler), resolve_dns=False
    )

    result = await provider.fetch(config, {})

    assert result.dispatched is False
    assert "密钥" in (result.error or "")


async def test_disabled_config_is_not_called() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        return httpx.Response(200, json={"card": "X"})

    result = await _provider(handler).fetch(_config(enabled=False), {})

    assert result.dispatched is False
    assert "已停用" in (result.error or "")


async def test_body_variables_are_rendered_into_the_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"card": "OK-1"})

    config = _config(body='{"order": "{{order_id}}", "qty": "{{quantity}}"}')

    result = await _provider(handler).fetch(config, {"order_id": "O-9", "quantity": "2"})

    assert result.ok is True
    assert captured["body"] == '{"order":"O-9","qty":"2"}'


@pytest.mark.parametrize("path", ["card", "data.card"])
async def test_simple_and_nested_paths(path: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"card": "A", "data": {"card": "B"}})

    result = await _provider(handler).fetch(_config(response_path=path), {})

    assert result.content == ("A" if path == "card" else "B")
