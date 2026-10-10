"""Wire and outcome tests for the platform confirm-shipment client (uncalibrated)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from xianyu_agent.protocol.mtop.endpoints.consign import (
    CONSIGN_API,
    CONSIGN_URL,
    ConsignError,
    ConsignRejectedError,
    ConsignUncertainError,
    XianyuConsignClient,
    build_consign_data,
    build_consign_headers,
    build_consign_params,
)

COOKIE = "unb=1; _m_h5_tk=abc123_1699999999999; _m_h5_tk_enc=xyz"


class _Signer:
    def __init__(self, cookie: str = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie


def test_consign_wire_shape_matches_the_reference() -> None:
    data = build_consign_data("O-1", trade_text="卡密A", pic_list=("https://img/x.png",))

    assert json.loads(data) == {
        "orderId": "O-1",
        "tradeText": "卡密A",
        "picList": ["https://img/x.png"],
        "newUnconsign": True,
    }
    params = build_consign_params("token-value", data, timestamp="1699999999999")
    assert params["api"] == CONSIGN_API
    assert params["v"] == "1.0"
    assert params["t"] == "1699999999999"
    assert params["sign"]
    assert "valueType" not in params


def test_consign_headers_stay_on_the_goofish_origin() -> None:
    headers = build_consign_headers(COOKIE)

    assert headers["Origin"] == "https://www.goofish.com"
    assert headers["Referer"] == "https://www.goofish.com/"
    assert headers["Accept"] == "application/json"
    assert headers["Cache-Control"] == "no-cache"
    assert headers["Pragma"] == "no-cache"
    assert headers["Priority"] == "u=1, i"
    assert headers["Sec-Fetch-Mode"] == "cors"
    assert headers["Cookie"] == COOKIE


def test_consign_data_rejects_an_empty_order_id() -> None:
    with pytest.raises(ValueError, match="order_id"):
        build_consign_data("   ")


@pytest.mark.asyncio
@respx.mock
async def test_consign_posts_signed_form_and_reports_success() -> None:
    route = respx.post(url__startswith=CONSIGN_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"]})
    )
    client = XianyuConsignClient(_Signer())  # type: ignore[arg-type]

    result = await client.confirm_delivery("acc-1", "O-1", trade_text="卡密A")

    request = route.calls.last.request
    assert result.confirmed is True
    assert result.order_id == "O-1"
    assert result.confirmed_at is not None
    assert "api=" + CONSIGN_API in str(request.url)
    assert "sign=" in str(request.url)
    assert request.headers["Referer"] == "https://www.goofish.com/"
    assert request.content.decode().startswith("data=")


@pytest.mark.asyncio
@respx.mock
async def test_consign_business_error_is_a_deterministic_rejection() -> None:
    respx.post(url__startswith=CONSIGN_URL).mock(
        return_value=httpx.Response(
            200, json={"ret": ["FAIL_BIZ_ORDER_STATUS_ERROR::订单状态错误"]}
        )
    )
    client = XianyuConsignClient(_Signer())  # type: ignore[arg-type]

    with pytest.raises(ConsignRejectedError, match="订单状态错误"):
        await client.confirm_delivery("acc-1", "O-1")


@pytest.mark.asyncio
async def test_consign_fails_closed_without_credentials_or_signing_token() -> None:
    without_cookie = XianyuConsignClient(_Signer(""))  # type: ignore[arg-type]
    with pytest.raises(ConsignError, match="无可用 Cookie"):
        await without_cookie.confirm_delivery("acc-1", "O-1")

    without_token = XianyuConsignClient(_Signer("unb=1"))  # type: ignore[arg-type]
    with pytest.raises(ConsignError, match="_m_h5_tk"):
        await without_token.confirm_delivery("acc-1", "O-1")


@pytest.mark.asyncio
@respx.mock
async def test_consign_reports_transport_and_http_failures_as_uncertain() -> None:
    client = XianyuConsignClient(_Signer())  # type: ignore[arg-type]

    respx.post(url__startswith=CONSIGN_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ConsignUncertainError, match="结果不明"):
        await client.confirm_delivery("acc-1", "O-1")

    respx.post(url__startswith=CONSIGN_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ConsignUncertainError, match="HTTP 503"):
        await client.confirm_delivery("acc-1", "O-1")

    respx.post(url__startswith=CONSIGN_URL).mock(
        return_value=httpx.Response(200, text="not-json")
    )
    with pytest.raises(ConsignUncertainError, match="不是合法 JSON"):
        await client.confirm_delivery("acc-1", "O-1")
