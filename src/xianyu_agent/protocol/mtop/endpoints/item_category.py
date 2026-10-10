"""类目推荐(mtop.taobao.idle.kgraph.property.recommend)—— 未真实校准。

请求与响应字段按参考实现 Christ9038/Ydisks-Xianyu-Helper(Apache-2.0)观察到的线格式
独立重写:标题与描述都传关键词,响应从 data.categoryPredictResult 读取完整类目路径。

状态:尚未在真实账号上校准;可以构造请求并被测试固定,但在真实调用验证之前不得据此
宣称类目识别可用。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from xianyu_agent.domain.item.category import PublishCategory
from xianyu_agent.protocol.mtop.endpoints.item_publish import (
    ItemPublishError,
    UploadedImage,
    _browser_headers,
    _decoded,
    _image_payload,
)
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
)

CATEGORY_RECOMMEND_API = "mtop.taobao.idle.kgraph.property.recommend"
CATEGORY_RECOMMEND_VERSION = "2.0"
CATEGORY_RECOMMEND_URL = (
    f"https://h5api.m.goofish.com/h5/{CATEGORY_RECOMMEND_API}/{CATEGORY_RECOMMEND_VERSION}/"
)
CATEGORY_SPM_CNT = "a21ybx.publish.0.0"
RECOMMEND_SCENE = "newPublishChoice"
PUBLISH_SCENE_MAIN = "mainPublish"
CATEGORY_RESULT_KEY = "categoryPredictResult"


class ItemCategoryError(RuntimeError):
    """类目推荐失败(凭证、网络、平台或响应缺少完整类目)。"""


def build_category_recommend_data(
    *,
    title: str,
    description: str | None = None,
    images: Sequence[UploadedImage] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the recommendation payload; the keyword is used as title and description."""
    cleaned = title.strip()
    if not cleaned:
        msg = "类目关键词不能为空"
        raise ValueError(msg)
    data: dict[str, Any] = {
        "title": cleaned,
        "lockCpv": False,
        "multiSKU": False,
        "publishScene": PUBLISH_SCENE_MAIN,
        "scene": RECOMMEND_SCENE,
        "description": (description or cleaned).strip(),
        "uniqueCode": str(int((now or datetime.now(UTC)).timestamp() * 1_000_000)),
    }
    if images:
        data["imageInfos"] = [
            _image_payload(image, major=index == 0) for index, image in enumerate(images)
        ]
    return data


def build_category_recommend_params(
    token: str, data: str, *, timestamp: str | None = None
) -> dict[str, str]:
    """Build the signed query parameters for one recommendation request."""
    resolved_timestamp = timestamp or str(int(time.time() * 1000))
    sign = compute_sign(token, int(resolved_timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": resolved_timestamp,
        "sign": sign,
        "v": CATEGORY_RECOMMEND_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": CATEGORY_RECOMMEND_API,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": CATEGORY_SPM_CNT,
    }


def parse_category_recommendation(decoded: Mapping[str, Any]) -> PublishCategory:
    """Read the recommended category path, or explain why none is usable."""
    data = decoded.get("data")
    result = data.get(CATEGORY_RESULT_KEY) if isinstance(data, Mapping) else None
    if not isinstance(result, Mapping):
        msg = "未能自动识别商品类目,请调整标题或图片后重试"
        raise ItemCategoryError(msg)
    try:
        return PublishCategory(
            cat_id=str(result.get("catId", "")),
            cat_name=str(result.get("catName", "")),
            channel_cat_id=str(result.get("channelCatId", "")),
            tb_cat_id=str(result.get("tbCatId", "")),
        )
    except ValueError as exc:
        msg = "未能自动识别商品类目,请调整标题或图片后重试"
        raise ItemCategoryError(msg) from exc


class XianyuItemCategoryClient:
    """Recommend a publish category; requires an existing signed cookie."""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        url: str = CATEGORY_RECOMMEND_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._url = url
        self._timeout = timeout_s

    async def recommend_category(
        self,
        account_id: str,
        *,
        title: str,
        description: str | None = None,
        images: Sequence[UploadedImage] = (),
    ) -> PublishCategory:
        """Ask the platform for a complete category path for one keyword."""
        data = json.dumps(
            build_category_recommend_data(title=title, description=description, images=images),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise ItemCategoryError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise ItemCategoryError(msg)
        headers = _browser_headers(cookie, content_type="application/x-www-form-urlencoded")
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    self._url,
                    params=build_category_recommend_params(token, data),
                    content=urlencode({"data": data}),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            msg = f"类目推荐网络请求失败({type(exc).__name__})"
            raise ItemCategoryError(msg) from exc
        await observe_mtop_response(account_id, response)
        try:
            decoded = _decoded(response, label="类目推荐", require_success=True)
        except ItemPublishError as exc:
            # 共用响应校验抛出的是发布域错误,这里换算成类目域自己的错误类型。
            msg = str(exc)
            raise ItemCategoryError(msg) from exc
        return parse_category_recommendation(decoded)
