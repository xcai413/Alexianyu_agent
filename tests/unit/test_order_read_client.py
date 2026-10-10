"""订单附加只读接口的线格式、解析和失败闭合测试。"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx

from xianyu_agent.protocol.mtop.endpoints.order_read import (
    BASE_URL,
    CONSIGN_API,
    LOGISTICS_API,
    REFUND_API,
    OrderReadError,
    XianyuOrderReadClient,
    parse_consign_info,
    parse_logistics_info,
    parse_refund_info,
)
from xianyu_agent.protocol.mtop.endpoints.orders import XianyuOrdersClient
from xianyu_agent.protocol.mtop.signer import APP_KEY, compute_sign

COOKIE = "unb=1; _m_h5_tk=abc123_1699999999999; _m_h5_tk_enc=xyz"


class _Signer:
    async def load_cookie_value(self, _account_id: str) -> str:
        return COOKIE


def test_parse_consign_info_extracts_spec_and_delivery_capabilities() -> None:
    result = parse_consign_info(
        {
            "bizOrderInfoList": [
                {
                    "commonData": {
                        "orderId": "O-1",
                        "itemId": "I-1",
                        "needSerialNumber": True,
                    },
                    "itemVO": {
                        "title": "测试商品",
                        "itemInfoLines": [{"key": "规格", "value": "蓝色"}],
                    },
                    "priceVO": {"buyNum": 2},
                    "buyerInfoVO": {"name": "买家", "phone": "13800000000"},
                }
            ],
            "supportConsignType": ["DUMMY_CONSIGN"],
        }
    )

    assert result["order_id"] == "O-1"
    assert result["spec_name"] == "规格"
    assert result["spec_value"] == "蓝色"
    assert result["quantity"] == 2
    assert result["need_serial_number"] is True
    assert result["support_consign_type"] == ["DUMMY_CONSIGN"]


def test_parse_logistics_info_returns_latest_node_and_status() -> None:
    result = parse_logistics_info(
        {
            "detailViewList": [
                {
                    "tradeIdAsString": "O-1",
                    "orderCode": "SF123",
                    "status": {"statusDesc": "运输中"},
                    "detailList": [{"ocurrTimeStr": "今天", "standerdDesc": "已揽收"}],
                }
            ]
        }
    )

    assert result["trade_id"] == "O-1"
    assert result["mail_no"] == "SF123"
    assert result["status"] == "运输中"
    assert result["latest"] == result["nodes"][0]


def test_parse_refund_info_does_not_return_nested_raw_payload() -> None:
    result = parse_refund_info(
        {
            "refundId": "R-1",
            "refundStatus": "退款成功",
            "buyerInfoVO": {"phone": "13800000000"},
            "records": [{"status": "done", "nested": {"phone": "13800000000"}}],
        }
    )

    assert result["refundId"] == "R-1"
    assert result["records"] == [{"status": "done"}]
    assert "buyerInfoVO" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api", "method"),
    [
        (CONSIGN_API, "fetch_consign_info"),
        (LOGISTICS_API, "fetch_logistics"),
        (REFUND_API, "fetch_refund_record"),
    ],
)
async def test_order_read_client_posts_signed_read_request(api: str, method: str) -> None:
    client = XianyuOrderReadClient(_Signer())  # type: ignore[arg-type]

    with respx.mock(assert_all_called=False) as router:
        route = router.post(BASE_URL.format(api=api, version="1.0")).mock(
            return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {}})
        )
        result = await getattr(client, method)("acc-1", "O-1")

    assert isinstance(result, dict)
    assert route.calls[0].request.headers["origin"] == "https://seller.goofish.com"
    assert "sign=" in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_order_read_client_rejects_business_error_without_leaking_response() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(BASE_URL.format(api=LOGISTICS_API, version="1.0")).mock(
            return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_ILLEGAL_ACCESS::非法请求"]})
        )
        client = XianyuOrderReadClient(_Signer())  # type: ignore[arg-type]
        with pytest.raises(OrderReadError, match="订单信息读取失败"):
            await client.fetch_logistics("acc-1", "O-1")


def test_consign_preserves_all_specs_and_masked_contact() -> None:
    data = parse_consign_info(
        {
            "bizOrderInfoList": [
                {
                    "buyerInfoVO": {"name": "王*", "phone": "138****5678", "address": "浙江省***"},
                    "itemVO": {
                        "itemInfoLines": [
                            {"key": "商品ID", "value": "I-1"},
                            {"key": "颜色", "value": "蓝色"},
                            {"key": "尺码", "value": "M"},
                        ]
                    },
                }
            ]
        }
    )
    assert data["specifications"] == [
        {"name": "颜色", "value": "蓝色"},
        {"name": "尺码", "value": "M"},
    ]
    assert data["buyer_phone"] == "138****5678"
    assert data["buyer_address"] == "浙江省***"
    assert (
        parse_consign_info({"bizOrderInfoList": [{"itemVO": [], "buyerInfoVO": "bad"}]})[
            "buyer_name"
        ]
        == ""
    )


def test_logistics_retains_all_packages() -> None:
    data = parse_logistics_info(
        {
            "detailViewList": [
                {"orderCode": "SF1", "detailList": [{"desc": "已揽收"}]},
                {"orderCode": "SF2", "detailList": [{"desc": "已签收"}]},
            ]
        }
    )
    assert data["package_count"] == 2
    assert data["mail_no"] == "SF1"
    assert data["packages"][1]["nodes"][0]["desc"] == "已签收"


@pytest.mark.asyncio
async def test_order_read_sign_uses_token_seed_not_expiry_suffix() -> None:

    with respx.mock as router:
        route = router.post(BASE_URL.format(api=CONSIGN_API, version="1.0")).mock(
            return_value=httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {}})
        )
        await XianyuOrderReadClient(_Signer()).fetch_consign_info("acc-1", "O-1")
    request = route.calls[0].request
    payload = parse_qs(request.content.decode())["data"][0]
    assert request.url.params["sign"] == compute_sign(
        "abc123", int(request.url.params["t"]), APP_KEY, payload
    )


@pytest.mark.asyncio
async def test_details_keeps_independent_results_and_sanitizes_failure(monkeypatch) -> None:

    async def sold(self, account_id, order_id):
        assert (account_id, order_id) == ("acc-1", "O-1")
        return {"receiver_phone": "138****5678"}

    async def failed(self, account_id, order_id):
        raise OrderReadError("secret-cookie-and-response-must-not-leak")

    async def logistics(self, account_id, order_id):
        return {"packages": [{"mail_no": "SF1"}]}

    monkeypatch.setattr(XianyuOrdersClient, "fetch_order_info", sold)
    monkeypatch.setattr(XianyuOrderReadClient, "fetch_consign_info", failed)
    monkeypatch.setattr(XianyuOrderReadClient, "fetch_logistics", logistics)
    monkeypatch.setattr(XianyuOrderReadClient, "fetch_refund_record", failed)
    result = await XianyuOrderReadClient(_Signer()).fetch_details("acc-1", "O-1")
    assert result["order"]["data"]["receiver_phone"] == "138****5678"
    assert result["logistics"]["data"]["packages"][0]["mail_no"] == "SF1"
    assert result["consign"]["data"] is None
    assert result["refund"]["error"]
    assert "secret-cookie" not in str(result)


def test_consign_missing_requirements_are_not_claimed_as_false() -> None:
    empty = parse_consign_info({})
    assert empty["need_serial_number"] is None
    assert empty["need_upload_report"] is None
    data = parse_consign_info(
        {
            "bizOrderInfoList": [
                {"commonData": {"needSerialNumber": "false", "needUploadReport": "true"}}
            ]
        }
    )
    assert data["need_serial_number"] is False
    assert data["need_upload_report"] is True
