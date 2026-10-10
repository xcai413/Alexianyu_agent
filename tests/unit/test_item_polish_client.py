"""Wire and outcome tests for the polish client (reference-derived, not yet calibrated)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from xianyu_agent.protocol.mtop.endpoints.item_polish import (
    POLISH_API,
    POLISH_URL,
    ItemPolishError,
    XianyuItemPolishClient,
    build_polish_data,
    build_polish_headers,
    build_polish_params,
    is_duplicate_polish,
)

COOKIE = "_m_h5_tk=abcdef123456_1699999999999; unb=1"


class _Signer:
    def __init__(self, cookie: str | None = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie or ""


def test_polish_wire_shape_matches_the_two_references() -> None:
    data = build_polish_data("I-1")
    params = build_polish_params("token-value", data, timestamp="1699999999999")

    assert json.loads(data) == {"itemId": "I-1"}
    assert params["api"] == POLISH_API
    assert params["v"] == "2.0"
    assert params["t"] == "1699999999999"
    assert params["accountSite"] == "xianyu"
    assert params["sessionOption"] == "AutoLoginOnly"
    assert params["sign"]
    assert params["appKey"]


def test_polish_headers_stay_on_the_goofish_origin() -> None:
    headers = build_polish_headers(COOKIE)

    assert headers["Cookie"] == COOKIE
    assert headers["Origin"] == "https://www.goofish.com"
    assert headers["Referer"] == "https://www.goofish.com/"


def test_polish_data_rejects_an_empty_item_id() -> None:
    with pytest.raises(ValueError, match="item_id"):
        build_polish_data("   ")


@pytest.mark.parametrize(
    "ret",
    [
        ["FAIL_SYS_BIZ_ERROR::IDLEITEM_POLISH_AGAIN"],
        ["FAIL_BIZ_POLISH_DUPLICATE::重复擦亮"],
        ["FAIL_SYS_BIZ_ERROR::该商品一天只能擦亮一次"],
    ],
)
def test_duplicate_polish_markers_are_recognised(ret: list[str]) -> None:
    assert is_duplicate_polish(ret) is True


def test_unrelated_business_error_is_not_treated_as_duplicate() -> None:
    assert is_duplicate_polish(["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]) is False


@pytest.mark.asyncio
async def test_polish_posts_signed_form_and_reports_success() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(url__startswith=POLISH_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"]})
    )
    client = XianyuItemPolishClient(_Signer())  # type: ignore[arg-type]

    with router:
        result = await client.polish_item("acc-1", "I-1")
        request = route.calls[0].request

    assert result.polished is True
    assert result.already_polished is False
    assert result.polished_at is not None
    assert "%22itemId%22" in request.content.decode()
    assert "api=" + POLISH_API in str(request.url)
    assert "/2.0/" in str(request.url)


@pytest.mark.asyncio
async def test_polish_treats_already_polished_as_idempotent_success() -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__startswith=POLISH_URL).mock(
        return_value=httpx.Response(
            200, json={"ret": ["FAIL_SYS_BIZ_ERROR::IDLEITEM_POLISH_AGAIN"]}
        )
    )
    client = XianyuItemPolishClient(_Signer())  # type: ignore[arg-type]

    with router:
        result = await client.polish_item("acc-1", "I-1")

    assert result.polished is False
    assert result.already_polished is True


@pytest.mark.asyncio
async def test_polish_fails_closed_without_a_signing_token() -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__startswith=POLISH_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"]})
    )
    client = XianyuItemPolishClient(_Signer("unb=1"))  # type: ignore[arg-type]

    with router:
        with pytest.raises(ItemPolishError, match="_m_h5_tk"):
            await client.polish_item("acc-1", "I-1")
        assert not router.calls


@pytest.mark.asyncio
async def test_polish_reports_platform_and_transport_failures() -> None:
    client = XianyuItemPolishClient(_Signer())  # type: ignore[arg-type]
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=POLISH_URL).mock(
            return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]})
        )
        with pytest.raises(ItemPolishError, match="FAIL_SYS_ILLEGAL_ACCESS"):
            await client.polish_item("acc-1", "I-1")

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=POLISH_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(ItemPolishError, match="HTTP 状态异常"):
            await client.polish_item("acc-1", "I-1")

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=POLISH_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(ItemPolishError, match="网络请求失败"):
            await client.polish_item("acc-1", "I-1")
