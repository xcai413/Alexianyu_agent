"""批量铺货的结果导出与批量重排(对照 Helper 的 result.csv / retry-failed)。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from xianyu_agent.cli.commands import item as item_cli
from xianyu_agent.cli.main import app
from xianyu_agent.domain.item.publish_job import (
    INTERRUPTED_ERROR,
    BatchPublishEntry,
    BatchPublishJob,
    PublishDraft,
)

runner = CliRunner()


def _draft(title: str, row: int) -> PublishDraft:
    return PublishDraft(
        account_id="acc-1", title=title, price=Decimal("45"), quantity=1, source_row=row
    )


class _FakeBatchService:
    """只实现这两条命令用到的方法,便于断言"重排了哪些行"。"""

    def __init__(self, job: BatchPublishJob) -> None:
        self.job = job
        self.retried: list[int] = []

    async def load_job(self, _job_id: str) -> BatchPublishJob:
        return self.job

    async def retry_entry(self, _job_id: str, index: int) -> BatchPublishJob:
        self.retried.append(index)
        updated = tuple(
            replace(entry, state="pending", error=None) if entry.index == index else entry
            for entry in self.job.entries
        )
        self.job = replace(self.job, entries=updated, state="pending")
        return self.job


def _mixed_job() -> BatchPublishJob:
    return BatchPublishJob(
        id="batch-1",
        account_id="acc-1",
        entries=(
            BatchPublishEntry(
                index=0, draft=_draft("帆布包", 2), state="succeeded", external_item_id="I-9"
            ),
            BatchPublishEntry(
                index=1, draft=_draft("水杯", 3), state="failed", error="标题被平台拒绝"
            ),
            BatchPublishEntry(
                index=2, draft=_draft("台灯", 4), state="failed", error=INTERRUPTED_ERROR
            ),
            BatchPublishEntry(
                index=3, draft=_draft("椅子", 5), state="skipped", error="缺图片"
            ),
        ),
        state="pending",
    )


def test_batch_export_writes_csv_with_row_results(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(item_cli, "ItemBatchService", lambda: _FakeBatchService(_mixed_job()))
    target = tmp_path / "result.csv"

    result = runner.invoke(
        app, ["item", "batch", "export", "--job-id", "batch-1", "--out", str(target)]
    )

    assert result.exit_code == 0, result.output
    text = target.read_text(encoding="utf-8")
    assert "行,状态,标题,平台商品,错误" in text
    assert "帆布包" in text
    assert "I-9" in text
    assert "标题被平台拒绝" in text


def test_batch_export_prints_to_stdout_without_out(monkeypatch) -> None:
    monkeypatch.setattr(item_cli, "ItemBatchService", lambda: _FakeBatchService(_mixed_job()))

    result = runner.invoke(app, ["item", "batch", "export", "--job-id", "batch-1"])

    assert result.exit_code == 0, result.output
    assert "帆布包" in result.output
    assert "标题被平台拒绝" in result.output


def test_batch_retry_failed_requeues_only_plain_failures(monkeypatch) -> None:
    """执行中断的行可能已经在平台侧创建了商品,默认必须留着等人工核对。"""
    service = _FakeBatchService(_mixed_job())
    monkeypatch.setattr(item_cli, "ItemBatchService", lambda: service)

    result = runner.invoke(app, ["item", "batch", "retry-failed", "--job-id", "batch-1"])

    assert result.exit_code == 0, result.output
    assert sorted(service.retried) == [1, 3]
    assert "已重排 2 行" in result.output
    assert "待人工核对" in result.output


def test_batch_retry_failed_requires_confirmation_for_interrupted_rows(monkeypatch) -> None:
    job = BatchPublishJob(
        id="batch-1",
        account_id="acc-1",
        entries=(
            BatchPublishEntry(
                index=0, draft=_draft("台灯", 2), state="failed", error=INTERRUPTED_ERROR
            ),
        ),
        state="pending",
    )
    service = _FakeBatchService(job)
    monkeypatch.setattr(item_cli, "ItemBatchService", lambda: service)

    refused = runner.invoke(
        app, ["item", "batch", "retry-failed", "--job-id", "batch-1", "--include-interrupted"]
    )

    assert refused.exit_code == 1
    assert "人工核对" in refused.output
    assert service.retried == []

    allowed = runner.invoke(
        app,
        [
            "item",
            "batch",
            "retry-failed",
            "--job-id",
            "batch-1",
            "--include-interrupted",
            "--yes",
        ],
    )

    assert allowed.exit_code == 0, allowed.output
    assert service.retried == [0]
