"""发货 API 配置与模板渲染(Issue #5 Phase 6, §18/M21/M22)。"""

from __future__ import annotations

import pytest

from xianyu_agent.domain.delivery import (
    ApiConfigError,
    ApiDeliveryConfig,
    extract_content,
    render_body,
    render_headers,
)


def _config(**kwargs: object) -> ApiDeliveryConfig:
    payload: dict[str, object] = {"name": "发卡接口", "url": "https://cards.example.com/dispatch"}
    payload.update(kwargs)
    return ApiDeliveryConfig(**payload)  # type: ignore[arg-type]


def test_config_validation_rejects_unsafe_input() -> None:
    with pytest.raises(ApiConfigError, match="名称"):
        _config(name="  ")
    with pytest.raises(ApiConfigError, match="https"):
        _config(url="http://cards.example.com/x")
    with pytest.raises(ApiConfigError, match="只允许 GET/POST/PUT"):
        _config(method="DELETE")
    with pytest.raises(ApiConfigError, match="超时"):
        _config(timeout_seconds=120)
    with pytest.raises(ApiConfigError, match="点分字段名"):
        _config(response_path="data[0].card")
    with pytest.raises(ApiConfigError, match="请求头名称"):
        _config(headers={"  ": "1"})


def test_template_variables_are_whitelisted() -> None:
    config = _config(
        body='{"order": "{{order_id}}", "qty": "{{quantity}}"}',
        headers={"X-Order": "{{order_id}}"},
    )

    assert config.variables == ("order_id", "quantity")

    with pytest.raises(ApiConfigError, match="不在白名单内"):
        _config(body='{"boom": "{{__import__}}"}')
    with pytest.raises(ApiConfigError, match="必须带引用名"):
        _config(headers={"Authorization": "{{secret:}}"})


def test_body_must_render_to_json_object() -> None:
    with pytest.raises(ApiConfigError, match="合法 JSON"):
        _config(body="{not json")

    with pytest.raises(ApiConfigError, match="JSON 对象"):
        _config(body='["a"]')


def test_secret_refs_are_collected_without_values() -> None:
    config = _config(
        headers={"Authorization": "Bearer {{secret:card_api_key}}"},
        body='{"key": "{{secret:card_api_key}}", "order": "{{order_id}}"}',
    )

    assert config.secret_refs == ("card_api_key",)
    assert "card_api_key" in config.describe() or "cards.example.com" in config.describe()


def test_render_substitutes_business_values_and_secrets() -> None:
    config = _config(
        headers={"Authorization": "Bearer {{secret:key}}", "X-Order": "{{order_id}}"},
        body='{"order": "{{order_id}}", "qty": "{{quantity}}", "key": "{{secret:key}}"}',
    )

    headers = render_headers(config, {"order_id": "O-1", "quantity": "2"}, {"key": "SEKRET"})
    body = render_body(config, {"order_id": "O-1", "quantity": "2"}, {"key": "SEKRET"})

    assert headers["Authorization"] == "Bearer SEKRET"
    assert headers["X-Order"] == "O-1"
    assert body == {"order": "O-1", "qty": "2", "key": "SEKRET"}


def test_render_requires_every_referenced_secret() -> None:
    config = _config(headers={"Authorization": "Bearer {{secret:key}}"})

    with pytest.raises(ApiConfigError, match="缺少密钥引用"):
        render_headers(config, {}, {})


def test_extract_content_walks_dotted_paths() -> None:
    payload = {"data": {"card": " CODE-9 ", "nested": {"code": "X"}}}

    assert extract_content(payload, "data.card") == "CODE-9"
    assert extract_content(payload, "data.nested.code") == "X"
    assert extract_content(payload, "data.missing") is None
    assert extract_content(payload, "data.blank") is None
    assert extract_content({"data": {"card": "  "}}, "data.card") is None
