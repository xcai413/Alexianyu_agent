"""Wire and parsing tests for the item detail client (reference-derived)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from xianyu_agent.protocol.mtop.endpoints.item_detail import (
    ITEM_DETAIL_API,
    ITEM_DETAIL_URL,
    ItemDetailError,
    XianyuItemDetailClient,
    build_item_detail_data,
    build_item_detail_headers,
    build_item_detail_params,
    describe_structure,
    detect_multi_spec,
    parse_item_detail,
)
from xianyu_agent.protocol.mtop.signer import APP_KEY, compute_sign

COOKIE = "_m_h5_tk=abcdef123456_1699999999999; unb=1"
SUCCESS_BODY = {
    "ret": ["SUCCESS::调用成功"],
    "data": {"sellerDO": {"sellerId": " 777 "}, "itemDO": {"itemId": "I-1"}},
}


class _Signer:
    def __init__(self, cookie: str | None = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie or ""


def test_detail_data_and_params_match_the_observed_shape() -> None:
    data = build_item_detail_data("I-1")
    params = build_item_detail_params("token-value", data, timestamp="1699999999999")

    assert json.loads(data) == {"itemId": "I-1"}
    assert params["api"] == ITEM_DETAIL_API
    assert params["v"] == "1.0"
    assert params["t"] == "1699999999999"
    assert params["accountSite"] == "xianyu"
    assert params["sessionOption"] == "AutoLoginOnly"
    assert params["spm_cnt"] == "a21ybx.item.0.0"
    assert params["sign"]


def test_detail_data_rejects_an_empty_item_id() -> None:
    with pytest.raises(ValueError, match="item_id"):
        build_item_detail_data("   ")


def test_detail_signature_uses_token_seed() -> None:
    params = build_item_detail_params("abcdef_1699999999999", "{}", timestamp="1699999999999")
    assert params["sign"] == compute_sign("abcdef", 1699999999999, APP_KEY, "{}")


def test_parse_all_calibrated_images_and_preserve_missing_shape() -> None:
    facts = parse_item_detail(
        {
            "itemDO": {
                "imageInfos": [
                    {"url": "https://img.example/1.png"},
                    {"url": " https://img.example/2.png "},
                    {"url": "https://img.example/1.png"},
                    {"url": None},
                    "invalid",
                    {"url": ""},
                ]
            }
        },
        item_id="I-1",
    )
    assert facts.images == ("https://img.example/1.png", "https://img.example/2.png")
    assert parse_item_detail({"itemDO": {"imageInfos": []}}, item_id="I-1").images == ()
    assert parse_item_detail({"itemDO": {}}, item_id="I-1").images is None
    assert parse_item_detail({"itemDO": {"imageInfos": "invalid"}}, item_id="I-1").images is None


def test_headers_scope_cookie_and_referer_to_the_item_page() -> None:
    headers = build_item_detail_headers(COOKIE, "I-1")

    assert headers["Cookie"] == COOKIE
    assert headers["Origin"] == "https://www.goofish.com"
    assert headers["Referer"] == "https://www.goofish.com/item?id=I-1"
    assert "User-Agent" in headers


@pytest.mark.asyncio
async def test_fetch_detail_posts_form_data_and_reads_facts() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(url__startswith=ITEM_DETAIL_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_BODY)
    )
    client = XianyuItemDetailClient(_Signer())  # type: ignore[arg-type]

    with router:
        facts = await client.fetch_item_detail("acc-1", "I-1")
        request = route.calls[0].request

    assert facts.item_id == "I-1"
    assert facts.publisher_id == "777"
    assert facts.multi_spec is False
    assert facts.fetched_at is not None
    assert request.headers["referer"] == "https://www.goofish.com/item?id=I-1"
    assert "%22itemId%22" in request.content.decode()
    assert "api=" + ITEM_DETAIL_API in str(request.url)


@pytest.mark.asyncio
async def test_fetch_detail_fails_closed_without_a_signing_token() -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__startswith=ITEM_DETAIL_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_BODY)
    )
    client = XianyuItemDetailClient(_Signer("unb=1"))  # type: ignore[arg-type]

    with router:
        with pytest.raises(ItemDetailError, match="_m_h5_tk"):
            await client.fetch_item_detail("acc-1", "I-1")
        assert not router.calls


@pytest.mark.asyncio
async def test_fetch_detail_reports_platform_and_transport_failures() -> None:
    client = XianyuItemDetailClient(_Signer())  # type: ignore[arg-type]
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=ITEM_DETAIL_URL).mock(
            return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]})
        )
        with pytest.raises(ItemDetailError, match="FAIL_SYS_ILLEGAL_ACCESS"):
            await client.fetch_item_detail("acc-1", "I-1")

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=ITEM_DETAIL_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(ItemDetailError, match="HTTP 状态异常"):
            await client.fetch_item_detail("acc-1", "I-1")

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=ITEM_DETAIL_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(ItemDetailError, match="网络请求失败"):
            await client.fetch_item_detail("acc-1", "I-1")


def test_parse_detail_tolerates_missing_optional_fields() -> None:
    facts = parse_item_detail({"itemDO": {"itemId": "I-1"}}, item_id="I-1")

    assert facts.publisher_id is None
    assert facts.multi_spec is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"isMultiSku": True}, True),
        ({"isMultiSku": False}, False),
        ({"multi_sku": "true"}, True),
        ({"skuList": [{"a": 1}]}, False),
        ({"skuList": [{"a": 1}, {"a": 2}]}, True),
        ({"skuProps": []}, False),
        ({"skuProps": [{"name": "颜色"}]}, True),
        ({"props": [{"name": "颜色"}]}, False),
        ({"skuDO": {"props": [{"name": "颜色"}]}}, True),
        ({"skuBase": {"properties": [{"name": "颜色"}]}}, True),
        ({"nested": [{"deeper": {"specifications": [{"v": 1}]}}]}, True),
    ],
)
def test_multi_spec_detection_rules(payload: dict, expected: bool) -> None:
    assert detect_multi_spec(payload) is expected


def test_multi_spec_detection_is_depth_bounded() -> None:
    payload: dict = {"skuProps": [{"x": 1}]}
    for _ in range(20):
        payload = {"nested": payload}

    assert detect_multi_spec(payload) is False


def test_structure_dump_keeps_paths_but_never_values() -> None:
    payload = {
        "sellerDO": {"sellerId": "777", "nick": "卖家"},
        "itemDO": {"price": 19.9, "soldCount": 3, "tags": ["a", "b"]},
        "hidden": None,
        "flags": {"multiSku": True},
    }

    shape = describe_structure(payload)

    assert shape["sellerDO"] == {"sellerId": "string", "nick": "string"}
    assert shape["itemDO"] == {
        "price": "number",
        "soldCount": "number",
        "tags": {"list_length": 2, "item": "string"},
    }
    assert shape["hidden"] == "null"
    assert shape["flags"] == {"multiSku": "bool"}
    dumped = json.dumps(shape, ensure_ascii=False)
    for leaked in ("777", "卖家", "19.9", 'multiSku": true'):
        assert leaked not in dumped


def test_structure_dump_truncates_instead_of_walking_forever() -> None:
    deep: dict[str, object] = {"leaf": "value"}
    for _ in range(8):
        deep = {"nested": deep}

    shape = describe_structure(deep)

    assert "truncated" in json.dumps(shape, ensure_ascii=False)


@pytest.mark.asyncio
async def test_detail_structure_fetch_returns_shapes_only() -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__startswith=ITEM_DETAIL_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_BODY)
    )
    client = XianyuItemDetailClient(_Signer())  # type: ignore[arg-type]

    with router:
        structure = await client.fetch_detail_structure("acc-1", "I-1")

    assert structure["sellerDO"] == {"sellerId": "string"}
    assert "777" not in json.dumps(structure)
