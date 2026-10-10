"""商品发布应用服务:把一项草稿变成平台商品。

流程是先上传图片、再调用发布接口;两者都属于写入类协议,按参考实现构造但尚未真实
校准,因此本服务不提供默认凭据、也不会在缺少签名 token 时降级——失败就如实失败。

图片引用先按本地路径解析,失败再交给素材库按名称解析;两者都解析不到就报错,而不是
猜一个地址:平台需要的是它自己的媒体地址,我们无法上传一张没有的图。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

from xianyu_agent.domain.item.category import PublishCategory
from xianyu_agent.domain.item.publish_job import PublishDraft
from xianyu_agent.protocol.mtop.endpoints.item_publish import (
    ItemPublishError,
    PublishItemRequest,
    PublishResult,
    UploadedImage,
    XianyuItemWriter,
    yuan_to_cents,
)

from .material_service import ItemMaterialService
from .platform_guard import AccountRiskGate

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
Publisher = Callable[[PublishDraft], Awaitable[str]]


class ItemPublishService:
    """Publish one draft: upload its images, then call the publish API."""

    def __init__(
        self,
        writer: XianyuItemWriter,
        *,
        materials: ItemMaterialService | None = None,
        category: PublishCategory | None = None,
        risk_gate: AccountRiskGate | None = None,
    ) -> None:
        self._writer = writer
        self._materials = materials
        self._category = category
        self._risk_gate = risk_gate or AccountRiskGate()

    def as_publisher(self) -> Publisher:
        """Adapt this service to the batch runner's per-draft callable."""

        async def publish(draft: PublishDraft) -> str:
            return (await self.publish(draft)).item_id

        return publish

    async def publish(self, draft: PublishDraft) -> PublishResult:
        """Publish one draft and return the platform result."""
        await self._risk_gate.ensure_ready(draft.account_id)
        images = await self.upload_images(draft)
        try:
            return await self._writer.publish_item(
                draft.account_id, self.build_request(draft, images)
            )
        except Exception as exc:
            await self._risk_gate.record_failure(draft.account_id, exc)
            raise

    async def upload_images(self, draft: PublishDraft) -> tuple[UploadedImage, ...]:
        """Upload every image of a draft, resolving material references first."""
        uploaded: list[UploadedImage] = []
        for reference in draft.images:
            resolved = await self._resolve_upload_path(reference, draft.account_id)
            if resolved is None:
                msg = f"图片需要先上传,既不是可读的本地文件,也不在素材库里: {reference}"
                raise ItemPublishError(msg)
            path = Path(resolved)
            content = await asyncio.to_thread(path.read_bytes)
            uploaded.append(
                await self._writer.upload_image(
                    draft.account_id,
                    filename=path.name,
                    content=content,
                    content_type=CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                )
            )
        return tuple(uploaded)

    def build_request(
        self, draft: PublishDraft, images: tuple[UploadedImage, ...]
    ) -> PublishItemRequest:
        """Build the publish request; money is converted to cents explicitly."""
        return PublishItemRequest(
            title=draft.title,
            images=images,
            price_cents=_price_cents(draft.price, field="售价"),
            quantity=max(draft.quantity, 1),
            description=draft.description,
            original_price_cents=(
                None
                if draft.original_price is None
                else _price_cents(draft.original_price, field="原价")
            ),
            category=self._category,
            postage_mode=draft.postage_mode,
            postage_cents=_price_cents(draft.postage, field="运费") if draft.postage else 0,
            virtual=True,
        )

    async def _resolve_upload_path(self, reference: str, account_id: str) -> str | None:
        candidate = Path(reference)
        if await asyncio.to_thread(candidate.is_file):
            return str(candidate)
        if self._materials is None:
            return None
        return await self._materials.resolve_path(account_id, reference)


def _price_cents(value: Decimal | None, *, field: str) -> int:
    if value is None:
        msg = f"{field}缺失,无法换算为分"
        raise ItemPublishError(msg)
    try:
        return yuan_to_cents(value)
    except ValueError as exc:
        msg = f"{field}无法换算为分: {value}"
        raise ItemPublishError(msg) from exc
