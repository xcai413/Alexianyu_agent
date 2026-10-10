"""Wire tests for the buyer-flower client (single-source reference, uncalibrated)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from xianyu_agent.protocol.mtop.endpoints.buyer_flower import (
    FLOWER_API,
    FLOWER_URL,
    SELLER_ORIGIN,
    BuyerFlowerError,
    XianyuBuyerFlowerClient,
    build_flower_data,
    build_flower_headers,
    build_flower_params,
)

COOKIE = "_m_h5_tk=abcdef123456_1699999999999; unb=1"


class _Signer:
    def __init__(self, cookie: str | None = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie or ""


def test_flower_wire_shape_matches_the_reference() -> None:
    data = build_flower_data("O-1")
    params = build_flower_params("token-value", data, timestamp="1699999999999")

    assert json.loads(data) == {"orderId": "O-1"}
    assert params["api"] == FLOWER_API
    assert params["v"] == "1.0"
    assert params["t"] == "1699999999999"
    # 参考实现对该接口不传 valueType
    assert "valueType" not in params
    assert params["sign"]


def test_flower_headers_use_the_seller_origin() -> None:
    headers = build_flower_headers(COOKIE)

    assert headers["Origin"] == SELLER_ORIGIN
    assert headers["Referer"] == f"{SELLER_ORIGIN}/"
    assert headers["Cookie"] == COOKIE


def test_flower_data_rejects_an_empty_order_id() -> None:
    with pytest.raises(ValueError, match="order_id"):
        build_flower_data("  ")


@pytest.mark.asyncio
async def test_request_flower_posts_signed_form_and_reports_success() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(url__startswith=FLOWER_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"]})
    )
    client = XianyuBuyerFlowerClient(_Signer())  # type: ignore[arg-type]

    with router:
        result = await client.request_flower("acc-1", "O-1")
        request = route.calls[0].request

    assert result.requested is True
    assert result.requested_at is not None
    assert "%22orderId%22" in request.content.decode()
    assert "api=" + FLOWER_API in str(request.url)


@pytest.mark.asyncio
async def test_request_flower_fails_closed_without_a_signing_token() -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__startswith=FLOWER_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"]})
    )
    client = XianyuBuyerFlowerClient(_Signer("unb=1"))  # type: ignore[arg-type]

    with router:
        with pytest.raises(BuyerFlowerError, match="_m_h5_tk"):
            await client.request_flower("acc-1", "O-1")
        assert not router.calls


@pytest.mark.asyncio
async def test_request_flower_reports_platform_and_transport_failures() -> None:
    client = XianyuBuyerFlowerClient(_Signer())  # type: ignore[arg-type]
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=FLOWER_URL).mock(
            return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]})
        )
        with pytest.raises(BuyerFlowerError, match="FAIL_SYS_ILLEGAL_ACCESS"):
            await client.request_flower("acc-1", "O-1")

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=FLOWER_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(BuyerFlowerError, match="HTTP 状态异常"):
            await client.request_flower("acc-1", "O-1")

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=FLOWER_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(BuyerFlowerError, match="网络请求失败"):
            await client.request_flower("acc-1", "O-1")
