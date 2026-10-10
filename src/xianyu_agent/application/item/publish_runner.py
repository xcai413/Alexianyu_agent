"""批量铺货的执行驱动:预览挡下不合格的行,再把其余行交给发布器。

发布器是注入的:把一项草稿变成平台商品 id(先上传图片、再调用发布接口)属于写入类协议,
在没有真实校准之前不提供默认实现,因此不注入发布器就无法执行——而不是静默跳过。

每行独立成败:一行失败不会中断整批,失败原因写回该行并进入审计可见的批次状态。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from xianyu_agent.domain.item.publish_job import PublishDraft

from .batch_service import ItemBatchService

logger = logging.getLogger(__name__)

Publisher = Callable[[PublishDraft], Awaitable[str]]
ImageResolver = Callable[[str, str], Awaitable[str | None]]
MAX_ERROR_LENGTH = 300

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class BatchRunSummary:
    """What one run did, for the caller's report."""

    job_id: str
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    stopped_reason: str | None = None


class ItemBatchRunner:
    """Drive one stored batch through an injected publisher."""

    def __init__(
        self,
        publisher: Publisher,
        *,
        service: ItemBatchService | None = None,
    ) -> None:
        self._publish = publisher
        self._service = service or ItemBatchService()

    async def run(self, job_id: str, *, max_rows: int | None = None) -> BatchRunSummary:
        """Preview, skip blocked rows, then publish the rest one row at a time."""
        job = await self._service.load_job(job_id)
        if job is None:
            msg = f"批次 {job_id} 不存在"
            raise ValueError(msg)
        skipped = await self._skip_blocked(job_id)
        attempted = succeeded = failed = 0
        stopped_reason: str | None = None
        while max_rows is None or attempted < max_rows:
            entry = await self._service.claim_next(job_id)
            if entry is None:
                break
            attempted += 1
            try:
                external_id = await self._publish(entry.draft)
            except Exception as exc:
                logger.warning("batch row failed type=%s", type(exc).__name__)
                failed += 1
                await self._service.mark_failed(job_id, entry.index, error=_error_text(exc))
                continue
            succeeded += 1
            await self._service.mark_succeeded(
                job_id, entry.index, external_item_id=external_id or None
            )
        if max_rows is not None and attempted >= max_rows:
            stopped_reason = f"达到本次上限 {max_rows} 行"
        return BatchRunSummary(
            job_id=job_id,
            attempted=attempted,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            stopped_reason=stopped_reason,
        )

    async def _skip_blocked(self, job_id: str) -> int:
        """Mark rows that preview already blocked, so they never reach the platform."""
        preview = await self._service.preview(job_id)
        skipped = 0
        for row in preview.rows:
            if not row.problems:
                continue
            reason = "; ".join(problem.detail for problem in row.problems)
            await self._service.mark_skipped(job_id, row.index, reason=reason)
            skipped += 1
        return skipped


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
