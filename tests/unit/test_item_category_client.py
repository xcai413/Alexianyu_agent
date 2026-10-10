"""Tests for category recommendation (reference-derived, not yet calibrated)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from xianyu_agent.application.item import ItemCategoryService
from xianyu_agent.domain.item.category import PublishCategory
from xianyu_agent.protocol.mtop.endpoints.item_category import (
    CATEGORY_RECOMMEND_API,
    CATEGORY_RECOMMEND_URL,
    ItemCategoryError,
    XianyuItemCategoryClient,
    build_category_recommend_data,
    build_category_recommend_params,
    parse_category_recommendation,
)
from xianyu_agent.protocol.mtop.endpoints.item_publish import UploadedImage

COOKIE = "_m_h5_tk=token123_1699999999999; unb=1"
SUCCESS_BODY = {
    "ret": ["SUCCESS::调用成功"],
    "data": {
        "categoryPredictResult": {
            "catId": "5001",
            "catName": "女包",
            "channelCatId": "88",
            "tbCatId": "50010001",
        }
    },
}


class _Signer:
    def __init__(self, cookie: str | None = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie or ""


def test_category_definition_requires_a_complete_path() -> None:
    category = PublishCategory(cat_id=" 5001 ", cat_name="女包", channel_cat_id="88")

    assert (category.cat_id, category.tb_cat_id) == ("5001", "")
    assert category.describe() == "女包(5001/频道 88)"
    with pytest.raises(ValueError, match="channel_cat_id"):
        PublishCategory(cat_id="5001", cat_name="女包", channel_cat_id="  ")


def test_recommend_data_and_params_match_the_observed_shape() -> None:
    data = build_category_recommend_data(
        title=" 帆布包 ",
        images=(UploadedImage(url="https://img.example/a.jpg"),),
        now=datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
    )
    params = build_category_recommend_params(
        "token-value", json.dumps(data, ensure_ascii=False), timestamp="1699999999999"
    )

    assert data["title"] == "帆布包"
    assert data["description"] == "帆布包"
    assert data["scene"] == "newPublishChoice"
    assert data["publishScene"] == "mainPublish"
    assert data["lockCpv"] is False
    assert data["imageInfos"][0]["major"] is True
    assert params["api"] == CATEGORY_RECOMMEND_API
    assert params["v"] == "2.0"
    assert params["spm_cnt"] == "a21ybx.publish.0.0"
    assert params["sign"]
    with pytest.raises(ValueError, match="关键词不能为空"):
        build_category_recommend_data(title="   ")


def test_parse_category_recommendation_requires_a_complete_result() -> None:
    category = parse_category_recommendation(SUCCESS_BODY)

    assert category.cat_id == "5001"
    assert category.channel_cat_id == "88"
    with pytest.raises(ItemCategoryError, match="未能自动识别商品类目"):
        parse_category_recommendation({"data": {}})
    with pytest.raises(ItemCategoryError, match="未能自动识别商品类目"):
        parse_category_recommendation({"data": {"categoryPredictResult": {"catId": "5001"}}})


@pytest.mark.asyncio
async def test_recommend_category_posts_form_data_and_reads_the_path() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(url__startswith=CATEGORY_RECOMMEND_URL).mock(
        return_value=httpx.Response(200, json=SUCCESS_BODY)
    )
    client = XianyuItemCategoryClient(_Signer())  # type: ignore[arg-type]

    with router:
        category = await client.recommend_category("acc-1", title="帆布包")
        request = route.calls[0].request

    assert category.cat_name == "女包"
    params = parse_qs(str(request.url).split("?", 1)[1])
    assert params["api"] == [CATEGORY_RECOMMEND_API]
    payload = json.loads(parse_qs(request.content.decode())["data"][0])
    assert payload["title"] == "帆布包"
    assert request.headers["cookie"] == COOKIE
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_recommend_category_fails_closed_and_maps_platform_errors() -> None:
    unsigned = XianyuItemCategoryClient(_Signer("unb=1"))  # type: ignore[arg-type]
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=CATEGORY_RECOMMEND_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_BODY)
        )
        with pytest.raises(ItemCategoryError, match="_m_h5_tk"):
            await unsigned.recommend_category("acc-1", title="帆布包")
        assert not router.calls

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=CATEGORY_RECOMMEND_URL).mock(
            return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]})
        )
        with pytest.raises(ItemCategoryError, match="平台返回失败"):
            await XianyuItemCategoryClient(_Signer()).recommend_category(  # type: ignore[arg-type]
                "acc-1", title="帆布包"
            )


@pytest.mark.asyncio
async def test_category_service_validates_the_keyword() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def recommend_category(
            self,
            account_id: str,
            *,
            title: str,
            description: str | None = None,
            images: object = (),
        ) -> PublishCategory:
            self.calls.append((account_id, title))
            return PublishCategory(cat_id="1", cat_name="包", channel_cat_id="2")

    client = _Client()
    service = ItemCategoryService(client)  # type: ignore[arg-type]

    category = await service.recommend("acc-1", keyword=" 帆布包 ")

    assert category.cat_id == "1"
    assert client.calls == [("acc-1", "帆布包")]
    with pytest.raises(ValueError, match="关键词不能为空"):
        await service.recommend("acc-1", keyword="  ")
