"""订单改价协议客户端:接线形状、金额换算与结果分类(未真实校准)。"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx

from xianyu_agent.protocol.mtop.endpoints.adjust_price import (
    ADJUST_PRICE_API,
    ADJUST_PRICE_URL,
    AdjustPriceError,
    AdjustPriceRejectedError,
    AdjustPriceUncertainError,
    XianyuAdjustPriceClient,
    build_adjust_price_data,
    build_adjust_price_headers,
    build_adjust_price_params,
    parse_price_to_cents,
)

COOKIE = "unb=1; _m_h5_tk=abc123_1699999999999; _m_h5_tk_enc=xyz"


class _Signer:
    def __init__(self, cookie: str = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie


def test_price_is_converted_to_integer_cents_without_float_error() -> None:
    assert parse_price_to_cents("12.34") == 1234
    assert parse_price_to_cents(Decimal("0.01")) == 1
    assert parse_price_to_cents("100") == 10000


@pytest.mark.parametrize("value", ["0", "-1", "12.345", "abc", ""])
def test_price_rejects_invalid_or_over_precise_amounts(value: str) -> None:
    with pytest.raises(ValueError):
        parse_price_to_cents(value)


def test_adjust_price_wire_shape_matches_the_reference() -> None:
    data = build_adjust_price_data("O-1", 1234)

    assert json.loads(data) == {
        "modifyFee": 1234,
        "newTransportFee": "0",
        "orderId": "O-1",
    }
    params = build_adjust_price_params("token-value", data, timestamp="1699999999999")
    assert params["api"] == ADJUST_PRICE_API
    assert params["v"] == "1.0"
    assert params["sign"]
    assert "spm_cnt" not in params


def test_adjust_price_headers_stay_on_the_goofish_origin() -> None:
    headers = build_adjust_price_headers(COOKIE)

    assert headers["Origin"] == "https://www.goofish.com"
    assert headers["Referer"] == "https://www.goofish.com/"


def test_adjust_price_data_rejects_non_positive_cents() -> None:
    with pytest.raises(ValueError, match="正整数"):
        build_adjust_price_data("O-1", 0)


@pytest.mark.asyncio
@respx.mock
async def test_adjust_price_requires_both_ret_success_and_data_success() -> None:
    route = respx.post(url__startswith=ADJUST_PRICE_URL).mock(
        return_value=httpx.Response(
            200, json={"ret": ["SUCCESS::调用成功"], "data": {"success": True}}
        )
    )
    client = XianyuAdjustPriceClient(_Signer())  # type: ignore[arg-type]

    result = await client.adjust_order_price("acc-1", "O-1", 1234)

    request = route.calls.last.request
    assert result.modified is True
    assert result.price_cents == 1234
    assert result.adjusted_at is not None
    assert "api=" + ADJUST_PRICE_API in str(request.url)
    assert request.content.decode().startswith("data=")


@pytest.mark.asyncio
@respx.mock
async def test_adjust_price_treats_success_ret_without_data_flag_as_rejection() -> None:
    respx.post(url__startswith=ADJUST_PRICE_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"]})
    )
    client = XianyuAdjustPriceClient(_Signer())  # type: ignore[arg-type]

    with pytest.raises(AdjustPriceRejectedError, match=r"data\.success"):
        await client.adjust_order_price("acc-1", "O-1", 1234)


@pytest.mark.asyncio
@respx.mock
async def test_adjust_price_reports_business_error_as_rejection() -> None:
    respx.post(url__startswith=ADJUST_PRICE_URL).mock(
        return_value=httpx.Response(
            200, json={"ret": ["FAIL_BIZ_ORDER_STATUS_ERROR::订单状态错误"]}
        )
    )
    client = XianyuAdjustPriceClient(_Signer())  # type: ignore[arg-type]

    with pytest.raises(AdjustPriceRejectedError, match="订单状态错误"):
        await client.adjust_order_price("acc-1", "O-1", 1234)


@pytest.mark.asyncio
async def test_adjust_price_fails_closed_without_signing_token() -> None:
    client = XianyuAdjustPriceClient(_Signer("unb=1"))  # type: ignore[arg-type]

    with pytest.raises(AdjustPriceError, match="_m_h5_tk"):
        await client.adjust_order_price("acc-1", "O-1", 1234)


@pytest.mark.asyncio
@respx.mock
async def test_adjust_price_reports_transport_failures_as_uncertain() -> None:
    client = XianyuAdjustPriceClient(_Signer())  # type: ignore[arg-type]

    respx.post(url__startswith=ADJUST_PRICE_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AdjustPriceUncertainError, match="结果不明"):
        await client.adjust_order_price("acc-1", "O-1", 1234)

    respx.post(url__startswith=ADJUST_PRICE_URL).mock(return_value=httpx.Response(502))
    with pytest.raises(AdjustPriceUncertainError, match="HTTP 502"):
        await client.adjust_order_price("acc-1", "O-1", 1234)
