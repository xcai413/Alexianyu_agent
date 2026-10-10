"""Persistence and resume tests for publish batches (Issue #4 M13)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.item import ItemBatchService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import BatchPublishJob, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.item.publish_job import INTERRUPTED_ERROR, PublishDraft
from xianyu_agent.domain.item.sku import ItemVariant, normalize_specs


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "publish.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _draft(title: str, *, price: Decimal | None = Decimal("45"), row: int = 2) -> PublishDraft:
    return PublishDraft(
        account_id="acc-1",
        title=title,
        price=price,
        quantity=2,
        description="全新",
        images=("https://img.example/a.jpg",),
        source_row=row,
        variants=(
            ItemVariant(
                specs=normalize_specs({"颜色": "红色"}),
                external_sku_id="SKU-RED",
                price=Decimal("45.00"),
                stock=2,
            ),
        ),
    )


def _service() -> ItemBatchService:
    return ItemBatchService(id_factory=lambda: "batch-1")


@pytest.mark.asyncio
async def test_create_job_persists_rows_and_previews(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    service = _service()

    job = await service.create_job("acc-1", [_draft("帆布包"), _draft("水杯", price=None, row=3)])
    preview = await service.preview(job.id)

    assert job.state == "pending"
    assert job.progress.total == 2
    assert job.progress.pending == 2
    assert [entry.draft.title for entry in job.entries] == ["帆布包", "水杯"]
    assert job.entries[0].draft.images == ("https://img.example/a.jpg",)
    assert job.entries[0].draft.variants[0].external_sku_id == "SKU-RED"
    assert job.entries[0].draft.price == Decimal("45")
    assert (preview.ready, preview.blocked) == (1, 1)
    async with db_mod.get_async_session() as session:
        row = (
            await session.execute(select(BatchPublishJob).where(BatchPublishJob.id == "batch-1"))
        ).scalar_one()
    assert (row.total, row.pending, row.state) == (2, 2, "pending")


@pytest.mark.asyncio
async def test_claim_complete_and_fail_settle_the_batch(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    service = _service()
    job = await service.create_job("acc-1", [_draft("帆布包"), _draft("水杯", row=3)])

    first = await service.claim_next(job.id)
    assert first is not None
    assert (first.index, first.state, first.attempts) == (0, "running", 1)
    after_success = await service.mark_succeeded(job.id, first.index, external_item_id="I-9")
    assert after_success.progress.succeeded == 1
    assert after_success.state == "running"

    second = await service.claim_next(job.id)
    assert second is not None
    assert second.index == 1
    finished = await service.mark_failed(job.id, second.index, error="标题被平台拒绝")

    assert finished.state == "completed"
    assert finished.finished_at is not None
    assert (finished.progress.succeeded, finished.progress.failed) == (1, 1)
    assert finished.entries[0].external_item_id == "I-9"
    assert finished.entries[1].error == "标题被平台拒绝"
    reloaded = await service.load_job(job.id)
    assert reloaded is not None
    assert reloaded.entries[1].error == "标题被平台拒绝"
    assert await service.claim_next(job.id) is None


@pytest.mark.asyncio
async def test_recover_marks_interrupted_rows_for_review(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    service = _service()
    job = await service.create_job("acc-1", [_draft("帆布包"), _draft("水杯", row=3)])
    claimed = await service.claim_next(job.id)
    assert claimed is not None

    recovered = await service.recover(job.id)

    assert recovered.entries[0].state == "failed"
    assert recovered.entries[0].error == INTERRUPTED_ERROR
    assert recovered.progress.failed == 1
    assert recovered.progress.pending == 1
    next_entry = await service.claim_next(job.id)
    assert next_entry is not None
    assert next_entry.index == 1


@pytest.mark.asyncio
async def test_cancel_stops_claiming_and_keeps_recorded_results(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    service = _service()
    job = await service.create_job("acc-1", [_draft("帆布包"), _draft("水杯", row=3)])
    first = await service.claim_next(job.id)
    assert first is not None
    await service.mark_succeeded(job.id, first.index, external_item_id="I-9")

    cancelled = await service.cancel(job.id)

    assert cancelled.state == "cancelled"
    assert cancelled.progress.succeeded == 1
    assert await service.claim_next(job.id) is None


@pytest.mark.asyncio
async def test_create_job_rejects_unknown_account_and_empty_drafts(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    service = _service()

    with pytest.raises(ValueError, match="至少需要一行"):
        await service.create_job("acc-1", [])
    with pytest.raises(ValueError, match="不存在"):
        await service.create_job("acc-missing", [_draft("帆布包")])


@pytest.mark.asyncio
async def test_retry_requeues_only_failed_or_skipped_rows(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    service = _service()
    job = await service.create_job("acc-1", [_draft("帆布包"), _draft("水杯", row=3)])
    first = await service.claim_next(job.id)
    assert first is not None
    await service.mark_failed(job.id, first.index, error="标题被平台拒绝")
    second = await service.claim_next(job.id)
    assert second is not None
    await service.mark_succeeded(job.id, second.index, external_item_id="I-9")
    assert (await service.load_job(job.id)).state == "completed"  # type: ignore[union-attr]

    requeued = await service.retry_entry(job.id, 0)

    assert requeued.state == "pending"
    assert requeued.finished_at is None
    assert requeued.entries[0].state == "pending"
    assert requeued.entries[0].error == "标题被平台拒绝"
    assert requeued.entries[0].attempts == 1
    assert requeued.progress.pending == 1

    claimed = await service.claim_next(job.id)

    assert claimed is not None
    assert claimed.index == 0
    assert claimed.error is None
    assert claimed.attempts == 2

    with pytest.raises(ValueError, match="只能重试失败或跳过"):
        await service.retry_entry(job.id, 1)
    await service.cancel(job.id)
    with pytest.raises(ValueError, match="已取消"):
        await service.retry_entry(job.id, 0)
