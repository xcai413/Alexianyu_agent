"""批量铺货的执行编排:建批次、预览、领取待办、回执与重启恢复。

真正的闲鱼发布调用属于写入类协议,由执行器注入;本服务只负责状态与持久化,
因此可以在完全离线的情况下测试。领域对象是唯一事实来源,数据库里的计数列是
在同一事务里写下的缓存,恢复流程会用条目重新对账。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace

from sqlalchemy import select

from xianyu_agent.db import Account, BatchPublishEntry, BatchPublishJob, get_async_session
from xianyu_agent.domain.item.publish_job import (
    BatchPreview,
    BatchPublishEntry as DomainEntry,
    BatchPublishJob as DomainJob,
    PublishDraft,
    draft_from_stored,
    draft_to_stored,
    preview_drafts,
)

IdFactory = Callable[[], str]


class ItemBatchService:
    """Drive one publish batch at a time."""

    def __init__(self, *, id_factory: IdFactory | None = None) -> None:
        self._new_id = id_factory or (lambda: uuid.uuid4().hex)

    async def create_job(self, account_id: str, drafts: Sequence[PublishDraft]) -> DomainJob:
        """Persist a new batch with one row per draft."""
        if not drafts:
            msg = "批次至少需要一行草稿"
            raise ValueError(msg)
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                msg = f"账号 {account_id} 不存在"
                raise ValueError(msg)
            job_id = self._new_id()
            session.add(
                BatchPublishJob(
                    id=job_id,
                    account_id=account.id,
                    state="pending",
                    total=len(drafts),
                    pending=len(drafts),
                    succeeded=0,
                    failed=0,
                    skipped=0,
                )
            )
            for index, draft in enumerate(drafts):
                session.add(
                    BatchPublishEntry(
                        job_id=job_id,
                        row_index=index,
                        state="pending",
                        draft=draft_to_stored(draft),
                    )
                )
            await session.commit()
        job = await self.load_job(job_id)
        if job is None:  # pragma: no cover - 刚写入的批次
            msg = f"批次 {job_id} 写入后不可读"
            raise RuntimeError(msg)
        return job

    async def load_job(self, job_id: str) -> DomainJob | None:
        """Read a batch and all of its rows as domain objects."""
        async with get_async_session() as session:
            job_row = (
                await session.execute(
                    select(BatchPublishJob).where(BatchPublishJob.id == job_id).limit(1)
                )
            ).scalar_one_or_none()
            if job_row is None:
                return None
            account = (
                await session.execute(
                    select(Account).where(Account.id == job_row.account_id).limit(1)
                )
            ).scalar_one_or_none()
            entry_rows = list(
                (
                    await session.execute(
                        select(BatchPublishEntry)
                        .where(BatchPublishEntry.job_id == job_id)
                        .order_by(BatchPublishEntry.row_index.asc())
                    )
                )
                .scalars()
                .all()
            )
        return DomainJob(
            id=job_row.id,
            account_id=account.account_id if account is not None else str(job_row.account_id),
            entries=tuple(_entry_from_row(row) for row in entry_rows),
            state=job_row.state,  # type: ignore[arg-type]
            created_at=job_row.created_at,
            started_at=job_row.started_at,
            finished_at=job_row.finished_at,
        )

    async def preview(self, job_id: str) -> BatchPreview:
        """Preview a stored batch without publishing anything."""
        job = await self._require_job(job_id)
        return preview_drafts(tuple(entry.draft for entry in job.entries))

    async def list_jobs(self, account_id: str, *, limit: int = 50) -> tuple[DomainJob, ...]:
        """List recent batches of one account, newest first."""
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return ()
            job_rows = list(
                (
                    await session.execute(
                        select(BatchPublishJob)
                        .where(BatchPublishJob.account_id == account.id)
                        .order_by(BatchPublishJob.created_at.desc(), BatchPublishJob.id.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not job_rows:
                return ()
            entry_rows = list(
                (
                    await session.execute(
                        select(BatchPublishEntry)
                        .where(BatchPublishEntry.job_id.in_([row.id for row in job_rows]))
                        .order_by(BatchPublishEntry.row_index.asc())
                    )
                )
                .scalars()
                .all()
            )
        grouped: dict[str, list[DomainEntry]] = {}
        for row in entry_rows:
            grouped.setdefault(row.job_id, []).append(_entry_from_row(row))
        return tuple(
            DomainJob(
                id=row.id,
                account_id=account.account_id,
                entries=tuple(grouped[row.id]),
                state=row.state,  # type: ignore[arg-type]
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
            )
            for row in job_rows
            # 没有行的批次不会被领域模型接受,也不会出现在列表里。
            if grouped.get(row.id)
        )

    async def claim_next(self, job_id: str) -> DomainEntry | None:
        """Mark the next pending row running and return it for execution."""
        job = await self._require_job(job_id)
        entry = job.next_pending()
        if entry is None:
            return None
        # 领取即清掉上一次的错误文本:这一行正在被重新尝试,旧的失败原因已经过期。
        claimed = replace(entry, state="running", attempts=entry.attempts + 1, error=None)
        await self._persist(job.start().with_entry(claimed))
        return claimed

    async def mark_succeeded(
        self, job_id: str, index: int, *, external_item_id: str | None = None
    ) -> DomainJob:
        """Record one row as published."""
        return await self._update_entry(
            job_id, index, state="succeeded", external_item_id=external_item_id, error=None
        )

    async def mark_failed(self, job_id: str, index: int, *, error: str) -> DomainJob:
        """Record one row as failed with a human-readable reason."""
        return await self._update_entry(job_id, index, state="failed", error=error)

    async def mark_skipped(self, job_id: str, index: int, *, reason: str) -> DomainJob:
        """Skip one row, e.g. when preview blocked it."""
        return await self._update_entry(job_id, index, state="skipped", error=reason)

    async def retry_entry(self, job_id: str, index: int) -> DomainJob:
        """Re-queue one failed or skipped row for another attempt.

        Retrying is per row and explicit: a row that failed because the process was
        interrupted comes back only when the operator confirms they checked the
        platform, never automatically. The previous error stays visible until the
        row is claimed again.
        """
        job = await self._require_job(job_id)
        if job.state == "cancelled":
            msg = f"批次 {job_id} 已取消,不能重试单行"
            raise ValueError(msg)
        current = next((entry for entry in job.entries if entry.index == index), None)
        if current is None:
            msg = f"批次 {job_id} 没有第 {index} 行"
            raise ValueError(msg)
        if current.state not in {"failed", "skipped"}:
            msg = f"第 {index} 行当前状态为 {current.state},只能重试失败或跳过的行"
            raise ValueError(msg)
        retried = tuple(
            replace(entry, state="pending") if entry.index == index else entry
            for entry in job.entries
        )
        updated = replace(job, entries=retried, state="pending", finished_at=None)
        await self._persist(updated)
        return updated

    async def recover(self, job_id: str) -> DomainJob:
        """Re-queue pending rows and flag rows interrupted mid-flight."""
        job = await self._require_job(job_id)
        recovered = job.recover_after_restart()
        await self._persist(recovered)
        return recovered

    async def cancel(self, job_id: str) -> DomainJob:
        """Stop handing out rows; recorded results stay as they are."""
        job = await self._require_job(job_id)
        cancelled = replace(job, state="cancelled")
        await self._persist(cancelled)
        return cancelled

    async def _update_entry(
        self,
        job_id: str,
        index: int,
        *,
        state: str,
        external_item_id: str | None = None,
        error: str | None = None,
    ) -> DomainJob:
        job = await self._require_job(job_id)
        current = next((entry for entry in job.entries if entry.index == index), None)
        if current is None:
            msg = f"批次 {job_id} 没有第 {index} 行"
            raise ValueError(msg)
        updated = replace(
            current,
            state=state,  # type: ignore[arg-type]
            external_item_id=(
                external_item_id if external_item_id is not None else current.external_item_id
            ),
            error=error,
        )
        job = job.with_entry(updated)
        await self._persist(job)
        return job

    async def _require_job(self, job_id: str) -> DomainJob:
        job = await self.load_job(job_id)
        if job is None:
            msg = f"批次 {job_id} 不存在"
            raise ValueError(msg)
        return job

    async def _persist(self, job: DomainJob) -> None:
        """Write the domain state and its derived counters in one transaction."""
        progress = job.progress
        async with get_async_session() as session:
            job_row = (
                await session.execute(
                    select(BatchPublishJob).where(BatchPublishJob.id == job.id).limit(1)
                )
            ).scalar_one_or_none()
            if job_row is None:
                msg = f"批次 {job.id} 不存在"
                raise ValueError(msg)
            rows = {
                row.row_index: row
                for row in (
                    await session.execute(
                        select(BatchPublishEntry).where(BatchPublishEntry.job_id == job.id)
                    )
                )
                .scalars()
                .all()
            }
            job_row.state = job.state
            job_row.total = progress.total
            job_row.pending = progress.pending
            job_row.succeeded = progress.succeeded
            job_row.failed = progress.failed
            job_row.skipped = progress.skipped
            job_row.started_at = job.started_at
            job_row.finished_at = job.finished_at
            for entry in job.entries:
                row = rows.get(entry.index)
                if row is None:
                    continue
                row.state = entry.state
                row.error = entry.error
                row.external_item_id = entry.external_item_id
                row.attempts = entry.attempts
            await session.commit()


def _entry_from_row(row: BatchPublishEntry) -> DomainEntry:
    return DomainEntry(
        index=row.row_index,
        draft=draft_from_stored(row.draft),
        state=row.state,  # type: ignore[arg-type]
        external_item_id=row.external_item_id,
        error=row.error,
        attempts=row.attempts,
    )
