"""Runner tests for the batch publish chain and its platform publisher."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from xianyu_agent.application.item import ItemBatchService
from xianyu_agent.application.item.publish_runner import (
    ItemBatchRunner,
)
from xianyu_agent.application.item.publish_service import ItemPublishService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.item.publish_job import PublishDraft
from xianyu_agent.protocol.mtop.endpoints.item_publish import (
    ItemPublishError,
    PublishResult,
    UploadedImage,
)


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "runner.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


class _FakeWriter:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes, str]] = []
        self.published: list[tuple[str, object]] = []

    async def upload_image(
        self, account_id: str, *, filename: str, content: bytes, content_type: str = "image/jpeg"
    ) -> UploadedImage:
        self.uploads.append((account_id, filename, content, content_type))
        return UploadedImage(url=f"https://img.example/{filename}", width=10, height=10)

    async def publish_item(self, account_id: str, request: object) -> PublishResult:
        self.published.append((account_id, request))
        return PublishResult(item_id="I-9", item_url="https://www.goofish.com/item?id=I-9")


def _draft(title: str = "帆布包", row: int = 2, **overrides: object) -> PublishDraft:
    values: dict[str, object] = {
        "account_id": "acc-1",
        "title": title,
        "price": Decimal("45"),
        "quantity": 2,
        "source_row": row,
    }
    values.update(overrides)
    return PublishDraft(**values)  # type: ignore[arg-type]


async def _create_job(drafts: list[PublishDraft]) -> str:
    job = await ItemBatchService(id_factory=lambda: "batch-1").create_job("acc-1", drafts)
    return job.id


@pytest.mark.asyncio
async def test_runner_skips_blocked_rows_and_publishes_the_rest(clean_db) -> None:
    job_id = await _create_job([_draft(row=2), _draft("水杯", row=3, price=None)])
    published: list[str] = []

    async def publisher(draft: PublishDraft) -> str:
        published.append(draft.title)
        return "I-9"

    summary = await ItemBatchRunner(publisher).run(job_id)
    job = await ItemBatchService().load_job(job_id)

    assert summary.attempted == 1
    assert summary.succeeded == 1
    assert summary.skipped == 1
    assert published == ["帆布包"]
    assert job is not None
    assert job.state == "completed"
    assert [entry.state for entry in job.entries] == ["succeeded", "skipped"]
    assert job.entries[0].external_item_id == "I-9"
    assert "价格不能为空" in (job.entries[1].error or "")


@pytest.mark.asyncio
async def test_runner_records_per_row_failure_and_keeps_going(clean_db) -> None:
    job_id = await _create_job([_draft(row=2), _draft("水杯", row=3)])

    async def publisher(draft: PublishDraft) -> str:
        if draft.title == "帆布包":
            msg = "平台拒绝"
            raise ItemPublishError(msg)
        return "I-10"

    summary = await ItemBatchRunner(publisher).run(job_id)
    job = await ItemBatchService().load_job(job_id)

    assert (summary.succeeded, summary.failed) == (1, 1)
    assert job is not None
    assert job.progress.failed == 1
    assert "ItemPublishError: 平台拒绝" in (job.entries[0].error or "")
    assert job.entries[1].external_item_id == "I-10"


@pytest.mark.asyncio
async def test_runner_stops_at_the_row_limit(clean_db) -> None:
    job_id = await _create_job([_draft(row=2), _draft("水杯", row=3), _draft("帽子", row=4)])

    async def publisher(_draft: PublishDraft) -> str:
        return "I-11"

    summary = await ItemBatchRunner(publisher).run(job_id, max_rows=2)
    job = await ItemBatchService().load_job(job_id)

    assert summary.attempted == 2
    assert summary.stopped_reason is not None
    assert "上限" in summary.stopped_reason
    assert job is not None
    assert job.progress.pending == 1


@pytest.mark.asyncio
async def test_runner_rejects_an_unknown_job(clean_db) -> None:
    async def publisher(_draft: PublishDraft) -> str:  # pragma: no cover - 不会调用
        return "I-1"

    with pytest.raises(ValueError, match="不存在"):
        await ItemBatchRunner(publisher).run("missing")


@pytest.mark.asyncio
async def test_platform_publisher_uploads_local_images_then_publishes(
    tmp_path: Path,
) -> None:
    image = tmp_path / "bag.png"
    image.write_bytes(b"png-bytes")
    draft = _draft(images=(str(image),), postage=Decimal("6"))
    writer = _FakeWriter()

    item_id = await ItemPublishService(writer).as_publisher()(draft)  # type: ignore[arg-type]
    request = writer.published[0][1]

    assert item_id == "I-9"
    assert writer.uploads == [("acc-1", "bag.png", b"png-bytes", "image/png")]
    assert request.price_cents == 4500  # type: ignore[attr-defined]
    assert request.original_price_cents is None  # type: ignore[attr-defined]
    assert request.postage_cents == 600  # type: ignore[attr-defined]
    assert request.images[0].url == "https://img.example/bag.png"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_platform_publisher_fails_on_unusable_images_and_prices(
    tmp_path: Path,
) -> None:
    writer = _FakeWriter()
    publish = ItemPublishService(writer).as_publisher()  # type: ignore[arg-type]

    with pytest.raises(ItemPublishError, match="不是可读的本地文件"):
        await publish(_draft(images=("https://img.example/a.jpg",)))
    with pytest.raises(ItemPublishError, match="售价缺失"):
        await publish(_draft(price=None))
    with pytest.raises(ItemPublishError, match="无法换算为分"):
        await publish(_draft(price=Decimal("45.555")))
    assert writer.published == []
