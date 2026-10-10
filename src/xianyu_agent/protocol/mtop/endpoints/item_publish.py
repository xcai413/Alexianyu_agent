"""商品写入类 mtop 接口的请求构造(未真实校准)。

本模块的字段按参考实现 Christ9038/Ydisks-Xianyu-Helper(Apache-2.0)观察到的线格式
独立实现:只写观察到的字段,没有观察到的字段一律不写、也不猜。价格与运费的平台单位
是分,与商品列表返回的字符串价格不是同一单位,两者的换算必须在调用方明确完成。

状态:尚未在真实账号上校准。可以构造请求并被测试固定,但在真实发布验证通过之前,
不得据此外发,也不得宣称已验收。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from xianyu_agent.domain.item.category import PublishCategory
from xianyu_agent.protocol.mtop.endpoints.items import _BROWSER_HEADERS as BROWSER_HEADERS
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
    make_headers,
)

PUBLISH_ITEM_API = "mtop.idle.pc.idleitem.publish"
PUBLISH_ITEM_VERSION = "1.0"
PUBLISH_ITEM_URL = f"https://h5api.m.goofish.com/h5/{PUBLISH_ITEM_API}/{PUBLISH_ITEM_VERSION}/"
UPLOAD_MEDIA_URL = "https://stream-upload.goofish.com/api/upload.api"
# 平台拼写的 floderId 是上游参数名,不要"修正"。
UPLOAD_MEDIA_QUERY: dict[str, str] = {
    "floderId": "0",
    "appkey": "xy_chat",
    "_input_charset": "utf-8",
}

PUBLISH_SCENE = "pcMainPublish"
ITEM_TYPE_STR = "b"
MAX_IMAGES = 9
PUBLISH_SPM_CNT = "a21ybx.publish.0.0"
ITEM_URL_TEMPLATE = "https://www.goofish.com/item?id={item_id}"
UPLOAD_IMAGE_DEFAULT_SIZE = 800
ITEM_ID_KEYS = ("itemId", "item_id", "id", "itemID")
ITEM_ID_FALLBACK_KEYS = ("itemId", "item_id", "itemID")
PostageMode = Literal["free", "fixed", "distance", "none"]
POSTAGE_MODES: tuple[PostageMode, ...] = ("free", "fixed", "distance", "none")


class ItemPublishError(RuntimeError):
    """发布或图片上传失败(凭证、网络、平台或响应格式)。"""


@dataclass(frozen=True)
class UploadedImage:
    """An image already uploaded to the platform media service."""

    url: str
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class PublishSku:
    """One spec combination with its own price and stock."""

    price_cents: int
    quantity: int
    properties: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class PublishItemRequest:
    """Everything the publish payload needs, with money in cents."""

    title: str
    images: tuple[UploadedImage, ...]
    price_cents: int = 0
    quantity: int = 1
    description: str | None = None
    original_price_cents: int | None = None
    category: PublishCategory | None = None
    postage_mode: PostageMode = "free"
    postage_cents: int = 0
    skus: tuple[PublishSku, ...] = ()
    virtual: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.price_cents < 0 or self.postage_cents < 0:
            msg = "金额不能为负数"
            raise ValueError(msg)
        if self.original_price_cents is not None and self.original_price_cents < 0:
            msg = "原价不能为负数"
            raise ValueError(msg)
        if self.quantity < 0:
            msg = "数量不能为负数"
            raise ValueError(msg)
        if self.postage_mode not in POSTAGE_MODES:
            msg = f"未知运费模式: {self.postage_mode}"
            raise ValueError(msg)


def validate_publish_request(request: PublishItemRequest) -> tuple[str, ...]:
    """Return locally provable problems; empty means the payload can be built.

    The rules mirror the platform behaviour observed in the reference
    implementation: title required, one to nine images, a positive price or
    per-spec prices, and a positive quantity. They are shape checks only; they
    do not mean the platform will accept the listing.
    """
    problems: list[str] = []
    if not request.title.strip():
        problems.append("商品标题不能为空")
    if not request.images:
        problems.append("至少需要 1 张已上传的商品图片")
    elif len(request.images) > MAX_IMAGES:
        problems.append(f"商品图片最多 {MAX_IMAGES} 张")
    if not request.skus and request.price_cents <= 0:
        problems.append("商品价格必须大于 0")
    # 参考实现先用请求里的数量校验,随后在多规格场景用规格汇总覆盖它,这里保持一致。
    if request.quantity <= 0:
        problems.append("库存数量必须大于 0")
    for sku in request.skus:
        if sku.price_cents <= 0:
            problems.append("多规格价格必须大于 0")
            break
    if request.postage_mode == "fixed" and request.postage_cents <= 0:
        problems.append("固定运费必须大于 0")
    return tuple(problems)


def build_publish_payload(request: PublishItemRequest) -> dict[str, Any]:
    """Build the publish body; raises when the request is not publishable."""
    problems = validate_publish_request(request)
    if problems:
        msg = "; ".join(problems)
        raise ValueError(msg)
    description = (request.description or "").strip() or request.title.strip()
    quantity = _sku_quantity(request.skus) if request.skus else request.quantity
    payload: dict[str, Any] = {
        "freebies": False,
        "itemTypeStr": ITEM_TYPE_STR,
        "quantity": str(quantity),
        "simpleItem": "true",
        "imageInfoDOList": [
            _image_payload(image, major=index == 0) for index, image in enumerate(request.images)
        ],
        "itemTextDTO": {
            "desc": description,
            "title": request.title.strip(),
            "titleDescSeparate": False,
        },
        "itemLabelExtList": [],
        "itemPriceDTO": _price_payload(request),
        "userRightsProtocols": [{"enable": False, "serviceCode": "SKILL_PLAY_NO_MIND"}],
        "itemPostFeeDTO": _postage_payload(request),
        "defaultPrice": False,
        "uniqueCode": str(int(time.time() * 1_000_000)),
        "sourceId": PUBLISH_SCENE,
        "bizcode": PUBLISH_SCENE,
        "publishScene": PUBLISH_SCENE,
    }
    if request.category is not None:
        payload["itemCatDTO"] = {
            "catId": request.category.cat_id,
            "catName": request.category.cat_name,
            "channelCatId": request.category.channel_cat_id,
            "tbCatId": request.category.tb_cat_id,
        }
    if request.skus:
        payload["itemSkuList"] = [
            {
                "priceInCent": str(sku.price_cents),
                "quantity": sku.quantity,
                "propertyList": [
                    {"propertyId": str(property_id), "propertyValueId": str(value)}
                    for property_id, value in sku.properties
                ],
            }
            for sku in request.skus
        ]
    payload.update(request.extra)
    return payload


def build_publish_params(token: str, data: str) -> dict[str, str]:
    """Build the signed query string for the publish request."""
    if token:
        signed = make_headers(token, data=data)
        timestamp = signed.x_t
        sign = signed.x_sign
    else:
        timestamp = str(int(time.time() * 1000))
        sign = compute_sign("", int(timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": timestamp,
        "sign": sign,
        "v": PUBLISH_ITEM_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": PUBLISH_ITEM_API,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": PUBLISH_SPM_CNT,
    }


def build_media_upload_url(base_url: str = UPLOAD_MEDIA_URL) -> str:
    """Return the media upload URL with the platform's required query parameters."""
    return f"{base_url}?{urlencode(UPLOAD_MEDIA_QUERY)}"


def build_media_upload_body(filename: str, content: bytes, content_type: str) -> tuple[bytes, str]:
    """Build a multipart body for one image; returns (body, content-type header).

    The platform expects a single "file" part, so the body is built by hand
    instead of pulling in a client that would also have to own Cookie handling.
    """
    boundary = f"----xianyuagent{int(time.time() * 1000)}"
    safe_name = filename.rsplit("/", 1)[-1].replace('"', "")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def token_from_cookie(cookie: str) -> str:
    """Read the mtop signing token out of a Cookie header value."""
    return extract_mtop_token(cookie)


def serialize_payload(payload: dict[str, Any]) -> str:
    """Render the payload the way the form field expects it: a JSON string."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _image_payload(image: UploadedImage, *, major: bool) -> dict[str, Any]:
    return {
        "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
        "isQrCode": False,
        "url": image.url,
        "heightSize": image.height,
        "widthSize": image.width,
        "major": major,
        "type": 0,
        "status": "done",
    }


def _price_payload(request: PublishItemRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not request.skus:
        payload["priceInCent"] = str(request.price_cents)
    if request.original_price_cents:
        payload["origPriceInCent"] = str(request.original_price_cents)
    return payload


def _postage_payload(request: PublishItemRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "canFreeShipping": False,
        "supportFreight": False,
        "onlyTakeSelf": False,
    }
    if request.postage_mode == "none":
        payload["templateId"] = "0"
    elif request.postage_mode == "free":
        payload["canFreeShipping"] = True
        payload["supportFreight"] = True
    elif request.postage_mode == "distance":
        payload["supportFreight"] = True
        payload["templateId"] = "-100"
    else:
        payload["supportFreight"] = True
        payload["postPriceInCent"] = str(request.postage_cents)
        payload["templateId"] = "0"
    return payload


def _sku_quantity(skus: tuple[PublishSku, ...]) -> int:
    return sum(max(sku.quantity, 0) for sku in skus)


def yuan_to_cents(value: Decimal) -> int:
    """Convert an operator-facing price in 元 to the platform's 分.

    A price that is not a whole number of 分 is rejected instead of rounded:
    silently rounding money is how a listing ends up cheaper than intended.
    """
    if value < 0:
        msg = "金额不能为负数"
        raise ValueError(msg)
    cents = (value * 100).to_integral_value()
    if cents != value * 100:
        msg = f"金额无法精确换算为分: {value}"
        raise ValueError(msg)
    return int(cents)


@dataclass(frozen=True)
class PublishResult:
    """What the platform returned for a successful publish."""

    item_id: str
    item_url: str
    data: dict[str, Any] = field(default_factory=dict)


def find_string_deep(value: Any, keys: tuple[str, ...]) -> str:
    """Return the first non-empty string for the given keys, shallowest first.

    The platform nests the new item id differently across publish scenes, so the
    lookup prefers the caller's keys at the current level and then recurses.
    """
    wanted = set(keys)
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in wanted:
                text = _as_text(child)
                if text:
                    return text
        for child in value.values():
            found = find_string_deep(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_string_deep(child, keys)
            if found:
                return found
    return ""


def parse_publish_result(decoded: Mapping[str, Any]) -> PublishResult:
    """Read the published item id from a publish response.

    A success response without an item id is treated as a failure on purpose:
    recording a publish we cannot reference would leave the batch row claiming a
    success nobody can trace back to a listing.
    """
    data = decoded.get("data")
    item_id = ""
    if isinstance(data, Mapping):
        item_id = find_string_deep(data, ITEM_ID_KEYS)
    if not item_id:
        item_id = find_string_deep(decoded, ITEM_ID_FALLBACK_KEYS)
    if not item_id:
        msg = "发布响应缺少商品 id"
        raise ItemPublishError(msg)
    return PublishResult(
        item_id=item_id,
        item_url=ITEM_URL_TEMPLATE.format(item_id=item_id),
        data=dict(data) if isinstance(data, Mapping) else {},
    )


def parse_media_upload(decoded: Mapping[str, Any]) -> UploadedImage:
    """Read the uploaded image url and size from an upload response."""
    obj = decoded.get("object")
    if not isinstance(obj, Mapping):
        fallback = decoded.get("data")
        obj = fallback if isinstance(fallback, Mapping) else {}
    url = _as_text(obj.get("url")) or _as_text(decoded.get("url"))
    if not url:
        msg = "图片上传响应缺少 url"
        raise ItemPublishError(msg)
    width, height = parse_pix(_as_text(obj.get("pix")))
    return UploadedImage(
        url=url,
        width=width or UPLOAD_IMAGE_DEFAULT_SIZE,
        height=height or UPLOAD_IMAGE_DEFAULT_SIZE,
    )


def parse_pix(pix: str) -> tuple[int, int]:
    """Parse the platform's pixel descriptor such as "800x800"."""
    width, separator, height = pix.partition("x")
    if not separator:
        return 0, 0
    try:
        return int(width), int(height)
    except ValueError:
        return 0, 0


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _browser_headers(cookie: str, *, content_type: str) -> dict[str, str]:
    """Browser headers for one product write, with a single Content-Type.

    The shared browser headers already carry a form Content-Type, so it is
    removed before setting the one this request needs; leaving both makes httpx
    merge them into one invalid header value.
    """
    headers = {key: value for key, value in BROWSER_HEADERS.items() if key != "Content-Type"}
    headers["Cookie"] = cookie
    headers["Content-Type"] = content_type
    return headers


class XianyuItemWriter:
    """Publish items and upload their images; requires an existing signed cookie.

    Not yet calibrated against a real account: the request shapes and response
    lookups follow the reference implementation, so treat every result as
    provisional until a real publish has been verified.
    """

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        publish_url: str = PUBLISH_ITEM_URL,
        upload_url: str = UPLOAD_MEDIA_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._publish_url = publish_url
        self._upload_url = upload_url
        self._timeout = timeout_s

    async def upload_image(
        self,
        account_id: str,
        *,
        filename: str,
        content: bytes,
        content_type: str = "image/jpeg",
    ) -> UploadedImage:
        """Upload one image and return the platform url with its size."""
        cookie, _token = await self._auth(account_id)
        body, multipart_type = build_media_upload_body(filename, content, content_type)
        headers = {**_browser_headers(cookie, content_type=multipart_type), "accept": "*/*"}
        response = await self._post(
            build_media_upload_url(self._upload_url), headers=headers, content=body
        )
        await observe_mtop_response(account_id, response)
        return parse_media_upload(_decoded(response, label="图片上传", require_success=False))

    async def publish_item(self, account_id: str, request: PublishItemRequest) -> PublishResult:
        """Publish one item; the request must already carry uploaded images."""
        payload = build_publish_payload(request)
        data = serialize_payload(payload)
        cookie, token = await self._auth(account_id)
        headers = _browser_headers(cookie, content_type="application/x-www-form-urlencoded")
        response = await self._post(
            self._publish_url,
            params=build_publish_params(token, data),
            headers=headers,
            content=urlencode({"data": data}),
        )
        await observe_mtop_response(account_id, response)
        return parse_publish_result(_decoded(response, label="商品发布", require_success=True))

    async def _auth(self, account_id: str) -> tuple[str, str]:
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise ItemPublishError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise ItemPublishError(msg)
        return cookie, token

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                return await client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            msg = f"商品写入网络请求失败({type(exc).__name__})"
            raise ItemPublishError(msg) from exc


def _decoded(response: httpx.Response, *, label: str, require_success: bool) -> Mapping[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        msg = f"{label} HTTP 状态异常({response.status_code})"
        raise ItemPublishError(msg)
    try:
        body = response.json()
    except ValueError as exc:
        msg = f"{label}响应不是合法 JSON"
        raise ItemPublishError(msg) from exc
    if not isinstance(body, Mapping):
        msg = f"{label}响应格式异常"
        raise ItemPublishError(msg)
    if require_success:
        ret = body.get("ret")
        if not isinstance(ret, list) or not any(str(entry).startswith("SUCCESS") for entry in ret):
            # 平台 ret 是排查失败原因的唯一线索,且不含凭据;把它带出来,否则真实失败
            # 只能看到一句"返回失败",无法判断是参数、权限还是会话问题。
            detail = ",".join(str(entry) for entry in ret) if isinstance(ret, list) else "无 ret"
            msg = f"{label}平台返回失败({detail[:160]})"
            raise ItemPublishError(msg)
    return body
