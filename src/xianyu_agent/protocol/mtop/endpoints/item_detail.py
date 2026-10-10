"""商品详情读取(mtop.taobao.idle.pc.detail)。

请求线格式(签名参数、form 编码的 data、Origin/Referer 作用域)与多规格判定规则来自
参考实现 Christ9038/Ydisks-Xianyu-Helper(Apache-2.0)的行为观察,独立用 Python 重写。

只使用有观察证据的响应字段:发布人标识、"是否多规格"与图片。
图片路径 itemDO.imageInfos[].url 已于 2026-10-10 真实只读校准;标题、价格仍不解析。

缺少签名 token 时直接失败,不发起未签名请求:bootstrap 由只读列表接口负责。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from xianyu_agent.protocol.mtop.endpoints.items import _BROWSER_HEADERS as BROWSER_HEADERS
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    derive_token_seed,
    extract_mtop_token,
)

ITEM_DETAIL_API = "mtop.taobao.idle.pc.detail"
ITEM_DETAIL_VERSION = "1.0"
ITEM_DETAIL_URL = f"https://h5api.m.goofish.com/h5/{ITEM_DETAIL_API}/{ITEM_DETAIL_VERSION}/"
ITEM_DETAIL_SPM = "a21ybx.item.0.0"
ITEM_PAGE_URL = "https://www.goofish.com/item?id={item_id}"
ITEM_PAGE_ORIGIN = "https://www.goofish.com"
MULTI_SPEC_DEPTH_LIMIT = 16


class ItemDetailError(RuntimeError):
    """商品详情读取失败(凭证、网络或平台业务错误)。"""


@dataclass(frozen=True)
class ItemDetailFacts:
    """Platform facts this client can currently name from a detail response."""

    item_id: str
    publisher_id: str | None = None
    multi_spec: bool = False
    fetched_at: datetime | None = None
    images: tuple[str, ...] | None = None


def build_item_detail_data(item_id: str) -> str:
    """Render the detail request body payload for one item."""
    if not item_id.strip():
        msg = "item_id 不能为空"
        raise ValueError(msg)
    return json.dumps({"itemId": item_id.strip()}, ensure_ascii=False, separators=(",", ":"))


def build_item_detail_params(
    token: str, data: str, *, timestamp: str | None = None
) -> dict[str, str]:
    """Build the signed query parameters for one detail request."""
    resolved_timestamp = timestamp or str(int(time.time() * 1000))
    sign = compute_sign(derive_token_seed(token), int(resolved_timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": resolved_timestamp,
        "sign": sign,
        "v": ITEM_DETAIL_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": ITEM_DETAIL_API,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": ITEM_DETAIL_SPM,
    }


def build_item_detail_headers(cookie: str, item_id: str) -> dict[str, str]:
    """Build request headers, scoping Cookie and Referer to this item page."""
    return {
        **BROWSER_HEADERS,
        "Cookie": cookie,
        "Origin": ITEM_PAGE_ORIGIN,
        "Referer": ITEM_PAGE_URL.format(item_id=item_id),
    }


def parse_item_detail(data: Mapping[str, Any], *, item_id: str) -> ItemDetailFacts:
    """Read the detail facts that have observed field paths."""
    seller = data.get("sellerDO")
    publisher: str | None = None
    if isinstance(seller, Mapping):
        raw = seller.get("sellerId")
        publisher = str(raw).strip() if raw not in (None, "") else None
    item = data.get("itemDO")
    raw_images = item.get("imageInfos") if isinstance(item, Mapping) else None
    images = (
        tuple(
            dict.fromkeys(
                row["url"].strip()
                for row in raw_images
                if isinstance(row, Mapping)
                and isinstance(row.get("url"), str)
                and row["url"].strip()
            )
        )
        if isinstance(raw_images, list)
        else None
    )
    return ItemDetailFacts(
        item_id=item_id,
        publisher_id=publisher,
        multi_spec=detect_multi_spec(data),
        fetched_at=datetime.now(UTC),
        images=images,
    )


def detect_multi_spec(value: Any) -> bool:
    """Whether a detail payload describes a multi-spec item.

    Keys are normalised by lower-casing and dropping underscores/hyphens, so
    multiSku, multi_sku and multi-sku all match. "props"/"properties" only count
    inside a SKU context, which is why the walk carries that flag down.
    """
    return _detect_multi_spec(value, sku_context=False, depth=0)


def _detect_multi_spec(value: Any, *, sku_context: bool, depth: int) -> bool:
    if depth > MULTI_SPEC_DEPTH_LIMIT:
        return False
    if isinstance(value, Mapping):
        return _detect_in_mapping(value, sku_context=sku_context, depth=depth)
    if isinstance(value, list):
        return any(
            _detect_multi_spec(child, sku_context=sku_context, depth=depth + 1) for child in value
        )
    return False


def _detect_in_mapping(value: Mapping[str, Any], *, sku_context: bool, depth: int) -> bool:
    for key, child in value.items():
        normalized = _normalize_key(str(key))
        if _marks_multi_spec(normalized, child, sku_context=sku_context):
            return True
        next_context = sku_context or normalized in {"skudo", "skubase", "skumodel"}
        if _detect_multi_spec(child, sku_context=next_context, depth=depth + 1):
            return True
    return False


def _marks_multi_spec(normalized_key: str, child: Any, *, sku_context: bool) -> bool:
    """Whether one key/child pair is itself evidence of a multi-spec item."""
    if normalized_key in {"multisku", "ismultisku", "ismultispec", "multiplesku"}:
        return _truthy(child)
    if normalized_key in {"skulist", "skus"}:
        return isinstance(child, list) and len(child) > 1
    if normalized_key in {"skuprops", "skuproperties", "specprops", "specifications"}:
        return isinstance(child, list) and bool(child)
    if normalized_key in {"props", "properties"}:
        return sku_context and isinstance(child, list) and bool(child)
    return False


def _normalize_key(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


STRUCTURE_DEPTH_LIMIT = 6
_SCALAR_SHAPES: tuple[tuple[type, str], ...] = (
    (bool, "bool"),
    (int, "number"),
    (float, "number"),
    (str, "string"),
)


def describe_structure(value: Any, *, max_depth: int = STRUCTURE_DEPTH_LIMIT) -> Any:
    """Describe a response's shape without any of its values.

    Calibrating an unverified endpoint needs the field paths, not the listing
    text, ids or credentials. This returns key names, value types and list
    lengths only, so a capture can be shared safely.
    """
    if max_depth <= 0:
        return "truncated"
    mapping_shape = _mapping_shape(value, max_depth=max_depth)
    if mapping_shape is not None:
        return mapping_shape
    if isinstance(value, list):
        return {
            "list_length": len(value),
            "item": (describe_structure(value[0], max_depth=max_depth - 1) if value else None),
        }
    for kind, shape in _SCALAR_SHAPES:
        if isinstance(value, kind):
            return shape
    return "null" if value is None else "unknown"


def _mapping_shape(value: Any, *, max_depth: int) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        str(key): describe_structure(child, max_depth=max_depth - 1) for key, child in value.items()
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


class XianyuItemDetailClient:
    """Read one item's detail facts; requires an existing signed cookie."""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        url: str = ITEM_DETAIL_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._url = url
        self._timeout = timeout_s

    async def fetch_item_detail(self, account_id: str, item_id: str) -> ItemDetailFacts:
        """Fetch the detail facts for one item of one account."""
        data = build_item_detail_data(item_id)
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise ItemDetailError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise ItemDetailError(msg)
        payload = await self._post(account_id, cookie, token, data)
        return parse_item_detail(payload, item_id=item_id.strip())

    async def fetch_detail_structure(self, account_id: str, item_id: str) -> Mapping[str, Any]:
        """Fetch the raw detail and return its shape with every value removed.

        This is the calibration tool for the unverified detail field paths: run it
        once against a real item and the result names every key without exposing
        the listing text or identifiers.
        """
        data = build_item_detail_data(item_id)
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise ItemDetailError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise ItemDetailError(msg)
        payload = await self._post(account_id, cookie, token, data)
        structure = describe_structure(payload)
        return structure if isinstance(structure, Mapping) else {"structure": structure}

    async def _post(self, account_id: str, cookie: str, token: str, data: str) -> Mapping[str, Any]:
        params = build_item_detail_params(token, data)
        headers = build_item_detail_headers(cookie, _item_id_from_data(data))
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    self._url,
                    params=params,
                    content=urlencode({"data": data}),
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            msg = f"商品详情网络请求失败({type(exc).__name__})"
            raise ItemDetailError(msg) from exc
        await observe_mtop_response(account_id, response)
        return _detail_payload(response)


def _item_id_from_data(data: str) -> str:
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:  # pragma: no cover - data 由本模块构造
        return ""
    return str(parsed.get("itemId", "")) if isinstance(parsed, dict) else ""


def _detail_payload(response: httpx.Response) -> Mapping[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        msg = f"商品详情 HTTP 状态异常({response.status_code})"
        raise ItemDetailError(msg)
    try:
        body = response.json()
    except ValueError as exc:
        msg = "商品详情响应不是合法 JSON"
        raise ItemDetailError(msg) from exc
    ret = body.get("ret") if isinstance(body, dict) else None
    if not isinstance(ret, list) or not any(str(entry).startswith("SUCCESS") for entry in ret):
        detail = ",".join(str(entry) for entry in ret) if isinstance(ret, list) else "无 ret"
        msg = f"商品详情平台返回失败({detail[:160]})"
        raise ItemDetailError(msg)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, Mapping):
        msg = "商品详情响应缺少 data"
        raise ItemDetailError(msg)
    return data
