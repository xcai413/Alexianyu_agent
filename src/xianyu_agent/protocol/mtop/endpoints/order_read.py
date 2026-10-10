"""订单附加只读接口。

接口事实来自 ``xcai413/xianyu-super-butler`` 的卖家端调用与解析代码:
发货页渲染、菜鸟物流轨迹、退款记录。这里只保留运营需要的结构化字段,
不把平台原始响应直接暴露给 CLI/Web。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from xianyu_agent.protocol.mtop.endpoints.orders import OrderSyncError, XianyuOrdersClient
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    derive_token_seed,
    extract_mtop_token,
)

SELLER_ORIGIN = "https://seller.goofish.com"
SELLER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
SELLER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": SELLER_ORIGIN,
    "Referer": f"{SELLER_ORIGIN}/",
    "User-Agent": SELLER_USER_AGENT,
}

CONSIGN_API = "mtop.taobao.idle.logistics.merchant.consign.page.render"
LOGISTICS_API = "mtop.cainiao.ld.detail.tradeid.ordercode.mailno.rescode.get.xy"
REFUND_API = "mtop.taobao.idle.merchant.refund.service.record"
API_VERSION = "1.0"
BASE_URL = "https://h5api.m.goofish.com/h5/{api}/{version}/"


class OrderReadError(RuntimeError):
    """订单附加信息读取失败。"""


def parse_consign_info(data: Mapping[str, Any]) -> dict[str, Any]:
    """提取发货页的规格、收货摘要和发货能力。"""
    orders = data.get("bizOrderInfoList") or []
    first = orders[0] if isinstance(orders, list) and orders else {}
    first = first if isinstance(first, Mapping) else {}

    def section(key: str) -> Mapping[str, Any]:
        value = first.get(key)
        return value if isinstance(value, Mapping) else {}

    common, item, price, buyer = (
        section(key) for key in ("commonData", "itemVO", "priceVO", "buyerInfoVO")
    )

    def text(value: Any) -> str:
        return str(value) if isinstance(value, (str, int, float, bool)) else ""

    def optional_bool(key: str) -> bool | None:
        value = common.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        return None

    spec_name = ""
    spec_value = ""
    lines = item.get("itemInfoLines") if isinstance(item, Mapping) else []
    specifications = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, Mapping):
            continue
        key = text(line.get("key"))
        if key and key != "商品ID":
            value = text(line.get("value"))
            specifications.append({"name": key, "value": value})
            if not spec_name:
                spec_name, spec_value = key, value
    return {
        "order_id": text(common.get("orderId")),
        "item_id": text(common.get("itemId")),
        "item_title": text(item.get("title")),
        "spec_name": spec_name,
        "spec_value": spec_value,
        "specifications": specifications,
        "quantity": price.get("buyNum") or 1,
        "buyer_name": text(buyer.get("name")),
        "buyer_phone": text(buyer.get("phone")),
        "buyer_address": text(buyer.get("address")),
        "need_serial_number": optional_bool("needSerialNumber"),
        "need_upload_report": optional_bool("needUploadReport"),
        "support_consign_type": [
            value for value in data.get("supportConsignType", []) if isinstance(value, str)
        ]
        if isinstance(data.get("supportConsignType"), list)
        else [],
    }


def parse_logistics_info(data: Mapping[str, Any]) -> dict[str, Any]:
    """保留第一包裹兼容字段,同时提取全部包裹的轨迹。"""
    views = data.get("detailViewList") or []
    packages = (
        [_parse_logistics_package(view) for view in views if isinstance(view, Mapping)]
        if isinstance(views, list)
        else []
    )
    result = dict(packages[0]) if packages else _parse_logistics_package({})
    return {**result, "package_count": len(packages), "packages": packages}


def _parse_logistics_package(view: Mapping[str, Any]) -> dict[str, Any]:
    nodes: list[dict[str, str]] = []
    details = view.get("detailList") or []
    for node in details if isinstance(details, list) else []:
        if not isinstance(node, Mapping):
            continue
        nodes.append(
            {
                "time": str(node.get("time") or node.get("ocurrTimeStr") or ""),
                "desc": str(node.get("desc") or node.get("standerdDesc") or ""),
                "action": str(node.get("action") or ""),
                "status_desc": str(node.get("statusDesc") or ""),
            }
        )
    status = view.get("status")
    if isinstance(status, Mapping):
        status_text = str(status.get("statusDesc") or status.get("statusCode") or "")
    else:
        status_text = str(status or "")
    return {
        "trade_id": str(view.get("tradeIdAsString") or view.get("tradeId") or ""),
        "mail_no": str(view.get("orderCode") or ""),
        "status": status_text,
        "nodes": nodes,
        "latest": nodes[0] if nodes else None,
    }


def parse_refund_info(data: Mapping[str, Any]) -> dict[str, Any]:
    """提取退款记录摘要,保留平台返回的业务字段但不保留原始载荷。"""
    result: dict[str, Any] = {}
    for key in (
        "refundId",
        "refundStatus",
        "status",
        "reason",
        "refundFee",
        "applyTime",
        "updateTime",
        "sellerRemark",
    ):
        if key in data and isinstance(data[key], (str, int, float, bool, type(None))):
            result[key] = data[key]
    for key in ("items", "refundList", "records"):
        value = data.get(key)
        if isinstance(value, list):
            result[key] = [
                {
                    str(k): v
                    for k, v in row.items()
                    if isinstance(v, (str, int, float, bool, type(None)))
                }
                for row in value
                if isinstance(row, Mapping)
            ]
    return result


class XianyuOrderReadClient:
    """读取订单附加信息;不执行任何平台写动作。"""

    def __init__(self, signer: CookieSigner | None = None, *, timeout_s: float = 20.0) -> None:
        self._signer = signer or CookieSigner()
        self._timeout = timeout_s

    async def fetch_consign_info(self, account_id: str, order_id: str) -> dict[str, Any]:
        return await self._fetch(
            account_id,
            order_id,
            CONSIGN_API,
            {"tradeId": order_id.strip()},
            parse_consign_info,
        )

    async def fetch_details(self, account_id: str, order_id: str) -> dict[str, Any]:
        """独立读取四种详情;单项失败不掩盖其余结果,不落库收货信息。"""

        async def read(method: Any) -> dict[str, Any]:
            try:
                return {"data": await method(account_id, order_id), "error": None}
            except (OrderReadError, OrderSyncError, ValueError):
                return {"data": None, "error": "平台信息读取失败,请稍后重试。"}

        values = await asyncio.gather(
            *(
                read(method)
                for method in (
                    XianyuOrdersClient(self._signer).fetch_order_info,
                    self.fetch_consign_info,
                    self.fetch_logistics,
                    self.fetch_refund_record,
                )
            )
        )
        return dict(zip(("order", "consign", "logistics", "refund"), values, strict=True))

    async def fetch_logistics(self, account_id: str, order_id: str) -> dict[str, Any]:
        return await self._fetch(
            account_id,
            order_id,
            LOGISTICS_API,
            {"tradeId": order_id.strip()},
            parse_logistics_info,
        )

    async def fetch_refund_record(self, account_id: str, order_id: str) -> dict[str, Any]:
        return await self._fetch(
            account_id,
            order_id,
            REFUND_API,
            {"orderId": order_id.strip()},
            parse_refund_info,
        )

    async def _fetch(
        self,
        account_id: str,
        order_id: str,
        api: str,
        payload: dict[str, str],
        parser: Any,
    ) -> dict[str, Any]:
        normalized = order_id.strip()
        if not normalized:
            raise ValueError("order_id 不能为空")
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            raise OrderReadError(f"账号 {account_id} 无可用 Cookie")
        token = extract_mtop_token(cookie)
        if not token:
            raise OrderReadError("Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": compute_sign(derive_token_seed(token), int(timestamp), APP_KEY, data),
            "v": API_VERSION,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21107h.42826273.0.0",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    BASE_URL.format(api=api, version=API_VERSION),
                    params=params,
                    content=urlencode({"data": data}),
                    headers={**SELLER_HEADERS, "Cookie": cookie},
                )
        except httpx.HTTPError as exc:
            raise OrderReadError(f"订单信息读取网络请求失败({type(exc).__name__})") from exc
        await observe_mtop_response(account_id, response)
        if response.status_code != 200:
            raise OrderReadError(f"订单信息读取 HTTP 状态异常: {response.status_code}")
        try:
            payload_value = response.json()
        except ValueError as exc:
            raise OrderReadError("订单信息读取响应不是合法 JSON") from exc
        if not isinstance(payload_value, Mapping):
            raise OrderReadError("订单信息读取响应结构异常")
        ret = payload_value.get("ret")
        if isinstance(ret, list) and not any("SUCCESS" in str(value) for value in ret):
            raise OrderReadError("订单信息读取失败: " + "; ".join(str(value) for value in ret))
        raw_data = payload_value.get("data")
        return parser(raw_data if isinstance(raw_data, Mapping) else {})
