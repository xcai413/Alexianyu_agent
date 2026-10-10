"""Wire-shape tests for publish requests (reference-derived, not yet calibrated)."""

from __future__ import annotations

import json

import pytest

from xianyu_agent.protocol.mtop.endpoints.item_publish import (
    MAX_IMAGES,
    PUBLISH_ITEM_API,
    PUBLISH_SCENE,
    PublishCategory,
    PublishItemRequest,
    PublishSku,
    UploadedImage,
    build_media_upload_body,
    build_media_upload_url,
    build_publish_params,
    build_publish_payload,
    serialize_payload,
    validate_publish_request,
)

IMAGE = UploadedImage(url="https://img.example/a.jpg", width=800, height=800)


def _request(**overrides: object) -> PublishItemRequest:
    values: dict[str, object] = {
        "title": "帆布包",
        "images": (IMAGE,),
        "price_cents": 4500,
        "quantity": 3,
    }
    values.update(overrides)
    return PublishItemRequest(**values)  # type: ignore[arg-type]


def test_simple_payload_matches_the_observed_wire_shape() -> None:
    payload = build_publish_payload(_request(description=" 全新未拆 "))

    assert set(payload) == {
        "freebies",
        "itemTypeStr",
        "quantity",
        "simpleItem",
        "imageInfoDOList",
        "itemTextDTO",
        "itemLabelExtList",
        "itemPriceDTO",
        "userRightsProtocols",
        "itemPostFeeDTO",
        "defaultPrice",
        "uniqueCode",
        "sourceId",
        "bizcode",
        "publishScene",
    }
    assert payload["quantity"] == "3"
    assert payload["simpleItem"] == "true"
    assert payload["sourceId"] == payload["bizcode"] == payload["publishScene"] == PUBLISH_SCENE
    assert payload["uniqueCode"].isdigit()
    assert payload["itemTextDTO"] == {
        "desc": "全新未拆",
        "title": "帆布包",
        "titleDescSeparate": False,
    }
    assert payload["itemPriceDTO"] == {"priceInCent": "4500"}
    assert payload["itemPostFeeDTO"] == {
        "canFreeShipping": True,
        "supportFreight": True,
        "onlyTakeSelf": False,
    }
    assert payload["imageInfoDOList"][0] == {
        "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
        "isQrCode": False,
        "url": "https://img.example/a.jpg",
        "heightSize": 800,
        "widthSize": 800,
        "major": True,
        "type": 0,
        "status": "done",
    }


def test_description_falls_back_to_title_and_category_is_included() -> None:
    category = PublishCategory(cat_id="1", cat_name="包", channel_cat_id="2", tb_cat_id="3")

    payload = build_publish_payload(_request(description=None, category=category))

    assert payload["itemTextDTO"]["desc"] == "帆布包"
    assert payload["itemCatDTO"] == {
        "catId": "1",
        "catName": "包",
        "channelCatId": "2",
        "tbCatId": "3",
    }


def test_multi_spec_payload_moves_price_into_the_sku_list() -> None:
    payload = build_publish_payload(
        _request(
            price_cents=0,
            quantity=1,
            original_price_cents=9900,
            skus=(
                PublishSku(price_cents=4500, quantity=2, properties=((1, "10"),)),
                PublishSku(price_cents=3900, quantity=1, properties=((1, "11"),)),
            ),
        )
    )

    assert payload["quantity"] == "3"
    assert payload["itemPriceDTO"] == {"origPriceInCent": "9900"}
    assert payload["itemSkuList"] == [
        {
            "priceInCent": "4500",
            "quantity": 2,
            "propertyList": [{"propertyId": "1", "propertyValueId": "10"}],
        },
        {
            "priceInCent": "3900",
            "quantity": 1,
            "propertyList": [{"propertyId": "1", "propertyValueId": "11"}],
        },
    ]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"title": "   "}, "标题"),
        ({"images": ()}, "至少需要 1 张"),
        ({"images": tuple(UploadedImage(url=f"u{i}") for i in range(MAX_IMAGES + 1))}, "最多 9 张"),
        ({"price_cents": 0}, "价格必须大于 0"),
        ({"quantity": 0}, "库存数量必须大于 0"),
        ({"postage_mode": "fixed", "postage_cents": 0}, "固定运费"),
    ],
)
def test_validation_reports_locally_provable_problems(
    overrides: dict[str, object], expected: str
) -> None:
    problems = validate_publish_request(_request(**overrides))

    assert any(expected in problem for problem in problems)
    with pytest.raises(ValueError, match=expected):
        build_publish_payload(_request(**overrides))


def test_postage_modes_render_the_observed_shapes() -> None:
    free = build_publish_payload(_request())
    fixed = build_publish_payload(_request(postage_mode="fixed", postage_cents=1200))
    distance = build_publish_payload(_request(postage_mode="distance"))
    none = build_publish_payload(_request(postage_mode="none"))

    assert free["itemPostFeeDTO"]["canFreeShipping"] is True
    assert fixed["itemPostFeeDTO"] == {
        "canFreeShipping": False,
        "supportFreight": True,
        "onlyTakeSelf": False,
        "postPriceInCent": "1200",
        "templateId": "0",
    }
    assert distance["itemPostFeeDTO"]["templateId"] == "-100"
    assert none["itemPostFeeDTO"]["templateId"] == "0"
    assert none["itemPostFeeDTO"]["supportFreight"] is False


def test_request_rejects_impossible_money_and_unknown_postage() -> None:
    with pytest.raises(ValueError, match="金额不能为负数"):
        _request(price_cents=-1)
    with pytest.raises(ValueError, match="未知运费模式"):
        _request(postage_mode="teleport")


def test_publish_params_carry_the_api_and_a_signature() -> None:
    signed = build_publish_params("token-value", '{"a":1}')
    anonymous = build_publish_params("", '{"a":1}')

    assert signed["api"] == PUBLISH_ITEM_API
    assert signed["v"] == "1.0"
    assert signed["t"].isdigit()
    assert signed["sign"]
    assert anonymous["sign"]
    assert signed["sign"] != anonymous["sign"]
    assert signed["dataType"] == "json"


def test_media_upload_url_and_multipart_body() -> None:
    url = build_media_upload_url()
    body, content_type = build_media_upload_body("images/a.jpg", b"jpeg-bytes", "image/jpeg")

    assert url.startswith("https://stream-upload.goofish.com/api/upload.api?")
    assert "floderId=0" in url
    assert "appkey=xy_chat" in url
    assert b'name="file"; filename="a.jpg"' in body
    assert b"Content-Type: image/jpeg" in body
    assert b"jpeg-bytes" in body
    assert body.endswith(b"--\r\n")
    boundary = content_type.split("boundary=")[1]
    assert boundary.encode() in body


def test_serialize_payload_is_compact_json() -> None:
    payload = build_publish_payload(_request())

    text = serialize_payload(payload)

    assert json.loads(text)["itemTextDTO"]["title"] == "帆布包"
    assert "帆布包" in text
    assert ", " not in text
