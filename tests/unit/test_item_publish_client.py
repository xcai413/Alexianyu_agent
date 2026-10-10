"""Client tests for the publish and image-upload wire paths (uncalibrated)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from xianyu_agent.protocol.mtop.endpoints.item_publish import (
    PUBLISH_ITEM_API,
    PUBLISH_ITEM_URL,
    UPLOAD_MEDIA_URL,
    ItemPublishError,
    PublishItemRequest,
    UploadedImage,
    XianyuItemWriter,
    find_string_deep,
    parse_media_upload,
    parse_pix,
    parse_publish_result,
)

COOKIE = "_m_h5_tk=token123_1699999999999; unb=1"
IMAGE = UploadedImage(url="https://img.example/a.jpg", width=800, height=800)


class _Signer:
    def __init__(self, cookie: str | None = COOKIE) -> None:
        self._cookie = cookie

    async def load_cookie_value(self, _account_id: str) -> str:
        return self._cookie or ""


def _request(**overrides: object) -> PublishItemRequest:
    values: dict[str, object] = {
        "title": "帆布包",
        "images": (IMAGE,),
        "price_cents": 4500,
        "quantity": 2,
    }
    values.update(overrides)
    return PublishItemRequest(**values)  # type: ignore[arg-type]


def test_find_string_deep_prefers_shallow_keys_then_recurses() -> None:
    payload = {"outer": {"item_id": "I-2"}, "itemId": "I-1"}

    assert find_string_deep(payload, ("itemId", "item_id")) == "I-1"
    assert find_string_deep({"outer": {"deep": {"item_id": "I-3"}}}, ("itemId", "item_id")) == "I-3"
    assert find_string_deep({"outer": {"value": 5}}, ("itemId",)) == ""
    assert find_string_deep([{"a": {"itemId": ""}}], ("itemId",)) == ""


def test_parse_publish_result_reads_the_item_id() -> None:
    direct = parse_publish_result({"ret": ["SUCCESS::调用成功"], "data": {"itemId": "I-9"}})
    nested = parse_publish_result({"data": {"nested": {"item_id": "I-8"}}})
    top_level = parse_publish_result({"itemId": "I-7", "data": {}})

    assert direct.item_id == "I-9"
    assert direct.item_url == "https://www.goofish.com/item?id=I-9"
    assert nested.item_id == "I-8"
    assert top_level.item_id == "I-7"
    with pytest.raises(ItemPublishError, match="缺少商品 id"):
        parse_publish_result({"ret": ["SUCCESS::调用成功"], "data": {"ok": True}})


def test_parse_media_upload_reads_url_and_size() -> None:
    primary = parse_media_upload({"object": {"url": "https://img.example/a.jpg", "pix": "800x600"}})
    fallback = parse_media_upload({"data": {"url": "https://img.example/b.jpg"}})
    top_level = parse_media_upload({"url": "https://img.example/c.jpg", "object": {}})

    assert (primary.url, primary.width, primary.height) == ("https://img.example/a.jpg", 800, 600)
    assert (fallback.width, fallback.height) == (800, 800)
    assert top_level.url == "https://img.example/c.jpg"
    with pytest.raises(ItemPublishError, match="缺少 url"):
        parse_media_upload({"object": {"pix": "800x800"}})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("800x600", (800, 600)),
        ("80 x 60", (80, 60)),
        ("abc", (0, 0)),
        ("", (0, 0)),
        ("800X600", (0, 0)),
    ],
)
def test_parse_pix(raw: str, expected: tuple[int, int]) -> None:
    assert parse_pix(raw) == expected


@pytest.mark.asyncio
async def test_upload_image_posts_multipart_and_reads_the_url() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(url__startswith=UPLOAD_MEDIA_URL).mock(
        return_value=httpx.Response(
            200, json={"object": {"url": "https://img.example/new.jpg", "pix": "640x480"}}
        )
    )
    writer = XianyuItemWriter(_Signer())  # type: ignore[arg-type]

    with router:
        uploaded = await writer.upload_image(
            "acc-1", filename="images/bag.jpg", content=b"jpeg-bytes"
        )
        request = route.calls[0].request

    assert uploaded.url == "https://img.example/new.jpg"
    assert (uploaded.width, uploaded.height) == (640, 480)
    assert "floderId=0" in str(request.url)
    assert "appkey=xy_chat" in str(request.url)
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'filename="bag.jpg"' in request.content
    assert b"jpeg-bytes" in request.content
    assert request.headers["cookie"] == COOKIE


@pytest.mark.asyncio
async def test_publish_item_posts_form_data_and_reads_the_item_id() -> None:
    router = respx.mock(assert_all_called=False)
    route = router.post(url__startswith=PUBLISH_ITEM_URL).mock(
        return_value=httpx.Response(
            200, json={"ret": ["SUCCESS::调用成功"], "data": {"itemId": "I-9"}}
        )
    )
    writer = XianyuItemWriter(_Signer())  # type: ignore[arg-type]

    with router:
        result = await writer.publish_item("acc-1", _request())
        request = route.calls[0].request

    params = parse_qs(str(request.url).split("?", 1)[1])
    assert result.item_id == "I-9"
    assert params["api"] == [PUBLISH_ITEM_API]
    assert params["v"] == ["1.0"]
    assert params["spm_cnt"] == ["a21ybx.publish.0.0"]
    assert params["sign"][0]
    payload = json.loads(parse_qs(request.content.decode())["data"][0])
    assert payload["itemTextDTO"]["title"] == "帆布包"
    assert payload["itemPriceDTO"] == {"priceInCent": "4500"}
    assert payload["imageInfoDOList"][0]["major"] is True


@pytest.mark.asyncio
async def test_publish_fails_closed_without_a_token_and_on_bad_requests() -> None:
    router = respx.mock(assert_all_called=False)
    router.post(url__startswith=PUBLISH_ITEM_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {}})
    )
    unsigned = XianyuItemWriter(_Signer("unb=1"))  # type: ignore[arg-type]

    with router:
        with pytest.raises(ItemPublishError, match="_m_h5_tk"):
            await unsigned.publish_item("acc-1", _request())
        assert not router.calls
        writer = XianyuItemWriter(_Signer())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="至少需要 1 张"):
            await writer.publish_item("acc-1", _request(images=()))
        assert not router.calls


@pytest.mark.asyncio
async def test_publish_reports_platform_and_transport_failures() -> None:
    writer = XianyuItemWriter(_Signer())  # type: ignore[arg-type]
    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=PUBLISH_ITEM_URL).mock(
            return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]})
        )
        with pytest.raises(ItemPublishError, match="平台返回失败"):
            await writer.publish_item("acc-1", _request())

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith=PUBLISH_ITEM_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(ItemPublishError, match="网络请求失败"):
            await writer.publish_item("acc-1", _request())
