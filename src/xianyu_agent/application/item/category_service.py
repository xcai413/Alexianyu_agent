"""类目推荐服务:把关键词交给平台并返回完整类目路径。"""

from __future__ import annotations

from collections.abc import Sequence

from xianyu_agent.domain.item.category import PublishCategory
from xianyu_agent.protocol.mtop.endpoints.item_category import XianyuItemCategoryClient
from xianyu_agent.protocol.mtop.endpoints.item_publish import UploadedImage

from .platform_guard import AccountRiskGate


class ItemCategoryService:
    """Recommend a publish category for one keyword."""

    def __init__(
        self,
        client: XianyuItemCategoryClient | None = None,
        *,
        risk_gate: AccountRiskGate | None = None,
    ) -> None:
        self._client = client or XianyuItemCategoryClient()
        self._risk_gate = risk_gate or AccountRiskGate()

    async def recommend(
        self,
        account_id: str,
        *,
        keyword: str,
        images: Sequence[UploadedImage] = (),
    ) -> PublishCategory:
        """Recommend a category; the keyword doubles as title and description."""
        cleaned = keyword.strip()
        if not cleaned:
            msg = "类目关键词不能为空"
            raise ValueError(msg)
        await self._risk_gate.ensure_ready(account_id)
        try:
            return await self._client.recommend_category(
                account_id, title=cleaned, description=cleaned, images=images
            )
        except Exception as exc:
            await self._risk_gate.record_failure(account_id, exc)
            raise
