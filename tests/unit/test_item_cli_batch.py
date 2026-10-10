"""CLI adapter tests for the batch and lifecycle commands (Issue #4 Phase 4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from xianyu_agent.application.item.lifecycle_service import LifecyclePlan, LifecyclePlanRow
from xianyu_agent.cli.commands import item as item_cli
from xianyu_agent.cli.main import app
from xianyu_agent.domain.item.category import PublishCategory
from xianyu_agent.domain.item.entity import Item
from xianyu_agent.domain.item.lifecycle_policy import (
    LifecycleDecision,
    LifecyclePolicy,
)
from xianyu_agent.domain.item.publish_job import (
    BatchPublishEntry,
    BatchPublishJob,
    PublishDraft,
    preview_drafts,
)
from xianyu_agent.domain.item.sku import normalize_specs
from xianyu_agent.domain.item.sku_binding import SpecBinding
from xianyu_agent.protocol.mtop.endpoints.item_category import ItemCategoryError
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailFacts

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
CSV_TEXT = "标题,价格,库存\n帆布包,45,3\n,50,1\n"


def _draft(title: str = "帆布包", row: int = 2) -> PublishDraft:
    return PublishDraft(
        account_id="acc-1", title=title, price=Decimal("45"), quantity=1, source_row=row
    )


def _job(state: str = "pending") -> BatchPublishJob:
    return BatchPublishJob(
        id="batch-1",
        account_id="acc-1",
        entries=(
            BatchPublishEntry(index=0, draft=_draft(), state="succeeded", external_item_id="I-9"),
            BatchPublishEntry(
                index=1, draft=_draft("水杯", row=3), state="failed", error="标题被平台拒绝"
            ),
        ),
        state=state,  # type: ignore[arg-type]
    )


class FakeBatchService:
    def __init__(self, job: BatchPublishJob | None = None) -> None:
        self._job = job or _job()
        self.created: list[tuple[str, list[PublishDraft]]] = []
        self.retried: tuple[str, int] | None = None

    async def create_job(self, account_id: str, drafts: list[PublishDraft]) -> BatchPublishJob:
        self.created.append((account_id, list(drafts)))
        return self._job

    async def preview(self, _job_id: str):
        return preview_drafts(tuple(entry.draft for entry in self._job.entries))

    async def load_job(self, _job_id: str) -> BatchPublishJob | None:
        return self._job

    async def recover(self, _job_id: str) -> BatchPublishJob:
        return self._job

    async def cancel(self, _job_id: str) -> BatchPublishJob:
        return self._job

    async def retry_entry(self, job_id: str, index: int) -> BatchPublishJob:
        self.retried = (job_id, index)
        return self._job


class FakeLifecycleService:
    def __init__(self, *, executor_missing: bool = True) -> None:
        self.executor_missing = executor_missing
        self.applied = 0

    async def plan(
        self, account_id: str, *, policy: LifecyclePolicy, item_filter: object, **_: object
    ):
        item = Item(
            external_item_id="I-OLD",
            account_id=account_id,
            title="帆布包",
            price=Decimal("45"),
            listed_at=NOW,
        )
        return LifecyclePlan(
            account_id=account_id,
            rows=(LifecyclePlanRow(item=item, decision=LifecycleDecision("DELIST", "上架 30 天")),),
            generated_at=NOW,
        )

    async def apply(self, _plan: object, *, allow_delete: bool = False):
        if self.executor_missing:
            msg = "未配置商品动作执行器,拒绝执行"
            raise RuntimeError(msg)
        self.applied += 1


def test_batch_import_creates_a_job_and_prints_preview(monkeypatch, tmp_path: Path) -> None:
    service = FakeBatchService()

    def _factory() -> FakeBatchService:
        return service

    monkeypatch.setattr(item_cli, "ItemBatchService", _factory)
    path = tmp_path / "rows.csv"
    path.write_text(CSV_TEXT, encoding="utf-8")

    result = CliRunner().invoke(
        app, ["item", "batch", "import", "--account", "acc-1", "--file", str(path)]
    )

    assert result.exit_code == 0
    assert "批次 batch-1 已创建" in result.output
    assert "跳过 1 行" in result.output
    assert "缺少标题" in result.output
    assert service.created[0][0] == "acc-1"
    assert [draft.title for draft in service.created[0][1]] == ["帆布包"]


def test_batch_import_rejects_an_unsupported_format(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(item_cli, "ItemBatchService", FakeBatchService)
    path = tmp_path / "rows.pdf"
    path.write_bytes(b"%PDF")

    result = CliRunner().invoke(
        app, ["item", "batch", "import", "--account", "acc-1", "--file", str(path)]
    )

    assert result.exit_code == 1
    assert "不支持的表格格式" in result.output


def test_batch_show_prints_progress_and_rows(monkeypatch) -> None:
    monkeypatch.setattr(item_cli, "ItemBatchService", lambda: FakeBatchService(_job("running")))

    result = CliRunner().invoke(app, ["item", "batch", "show", "--job-id", "batch-1"])

    assert result.exit_code == 0
    assert "成功 1" in result.output
    assert "失败 1" in result.output
    assert "标题被平台拒绝" in result.output
    assert "I-9" in result.output


def test_batch_show_reports_a_missing_job(monkeypatch) -> None:
    class _Missing(FakeBatchService):
        async def load_job(self, _job_id: str) -> None:
            return None

    monkeypatch.setattr(item_cli, "ItemBatchService", _Missing)

    result = CliRunner().invoke(app, ["item", "batch", "show", "--job-id", "nope"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_lifecycle_plan_prints_dry_run_without_acting(monkeypatch) -> None:
    monkeypatch.setattr(item_cli, "ItemLifecycleService", FakeLifecycleService)

    result = CliRunner().invoke(app, ["item", "lifecycle", "plan", "--account", "acc-1"])

    assert result.exit_code == 0
    assert "Dry Run" in result.output
    assert "DELIST" in result.output
    assert "上架 30 天" in result.output


def test_lifecycle_apply_needs_yes_and_fails_closed_without_an_executor(monkeypatch) -> None:
    monkeypatch.setattr(item_cli, "ItemLifecycleService", FakeLifecycleService)

    preview = CliRunner().invoke(app, ["item", "lifecycle", "apply", "--account", "acc-1"])
    confirmed = CliRunner().invoke(
        app, ["item", "lifecycle", "apply", "--account", "acc-1", "--yes"]
    )

    assert preview.exit_code == 0
    assert "未加 --yes" in preview.output
    assert confirmed.exit_code == 1
    assert "未配置商品动作执行器" in confirmed.output


class FakeRunner:
    def __init__(self, _publisher: object, *, service: object = None) -> None:
        self.service = service
        self.calls: list[tuple[str, int | None]] = []

    async def run(self, job_id: str, *, max_rows: int | None = None):
        self.calls.append((job_id, max_rows))
        return SimpleNamespace(
            attempted=2, succeeded=1, failed=1, skipped=0, stopped_reason="达到本次上限 2 行"
        )


def test_batch_run_needs_yes_then_reports_the_summary(monkeypatch) -> None:
    runner = FakeRunner(None)
    monkeypatch.setattr(item_cli, "ItemBatchService", lambda: FakeBatchService(_job("running")))
    monkeypatch.setattr(item_cli, "ItemBatchRunner", lambda *args, **kwargs: runner)

    preview = CliRunner().invoke(app, ["item", "batch", "run", "--job-id", "batch-1"])
    assert preview.exit_code == 0
    assert "未加 --yes" in preview.output
    assert runner.calls == []

    confirmed = CliRunner().invoke(
        app, ["item", "batch", "run", "--job-id", "batch-1", "--yes", "--max-rows", "2"]
    )

    assert confirmed.exit_code == 0
    assert runner.calls == [("batch-1", 2)]
    assert "成功 1" in confirmed.output
    assert "达到本次上限" in confirmed.output


def test_item_category_command_reports_the_recommendation(monkeypatch) -> None:
    class _Service:
        async def recommend(self, account_id: str, *, keyword: str):
            assert (account_id, keyword) == ("acc-1", "帆布包")
            return PublishCategory(cat_id="5001", cat_name="女包", channel_cat_id="88")

    class _Failing:
        async def recommend(self, _account_id: str, *, keyword: str):
            raise ItemCategoryError("未能自动识别商品类目,请调整标题或图片后重试")

    monkeypatch.setattr(item_cli, "ItemCategoryService", _Service)
    ok = CliRunner().invoke(app, ["item", "category", "--account", "acc-1", "--keyword", "帆布包"])

    monkeypatch.setattr(item_cli, "ItemCategoryService", _Failing)
    failed = CliRunner().invoke(
        app, ["item", "category", "--account", "acc-1", "--keyword", "帆布包"]
    )

    assert ok.exit_code == 0
    assert "女包(5001/频道 88)" in ok.output
    assert failed.exit_code == 1
    assert "未能自动识别商品类目" in failed.output


def test_item_detail_command_records_the_multi_spec_flag(monkeypatch) -> None:
    class _Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def sync_facts(self, account_id: str, item_id: str):
            assert (account_id, item_id) == ("acc-1", "I-1")
            return ItemDetailFacts(item_id=item_id, publisher_id="777", multi_spec=True)

    class _Missing:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def sync_facts(self, _account_id: str, _item_id: str):
            return None

    monkeypatch.setattr(item_cli, "ItemDetailService", _Service)
    ok = CliRunner().invoke(app, ["item", "detail", "--account", "acc-1", "--item-id", "I-1"])

    monkeypatch.setattr(item_cli, "ItemDetailService", _Missing)
    missing = CliRunner().invoke(app, ["item", "detail", "--account", "acc-1", "--item-id", "I-1"])

    assert ok.exit_code == 0
    assert "多规格=Y" in ok.output
    assert "发布人=已识别" in ok.output
    assert missing.exit_code == 1
    assert "不在本地镜像里" in missing.output


def test_batch_retry_requeues_the_row_the_operator_sees(monkeypatch) -> None:
    class _Service(FakeBatchService):
        async def retry_entry(self, job_id: str, index: int) -> BatchPublishJob:
            self.retried = (job_id, index)
            return self._job

    class _Rejecting(FakeBatchService):
        async def retry_entry(self, _job_id: str, _index: int) -> BatchPublishJob:
            msg = "第 1 行当前状态为 succeeded,只能重试失败或跳过的行"
            raise ValueError(msg)

    service = _Service()
    monkeypatch.setattr(item_cli, "ItemBatchService", lambda: service)
    ok = CliRunner().invoke(app, ["item", "batch", "retry", "--job-id", "batch-1", "--row", "2"])

    monkeypatch.setattr(item_cli, "ItemBatchService", _Rejecting)
    rejected = CliRunner().invoke(
        app, ["item", "batch", "retry", "--job-id", "batch-1", "--row", "1"]
    )

    assert ok.exit_code == 0
    assert service.retried == ("batch-1", 1)
    assert "第 2 行已重新排队" in ok.output
    assert rejected.exit_code == 1
    assert "只能重试失败或跳过" in rejected.output


def test_item_bind_commands_configure_and_list_the_mapping(monkeypatch) -> None:
    captured: dict[str, object] = {}
    binding = _binding_for({"颜色": "红色"}, target=7, quantity=2)

    async def fake_upsert(account_id: str, item_id: str, incoming: object) -> tuple:
        captured["upsert"] = (account_id, item_id, incoming)
        return (incoming,)

    async def fake_list(_account_id: str, _item_id: str) -> tuple:
        return (binding,)

    async def fake_remove(account_id: str, item_id: str, specs: object) -> tuple:
        captured["remove"] = (account_id, item_id, specs)
        return ()

    monkeypatch.setattr(item_cli, "upsert_item_binding", fake_upsert)
    monkeypatch.setattr(item_cli, "list_item_bindings", fake_list)
    monkeypatch.setattr(item_cli, "remove_item_binding", fake_remove)

    bound = CliRunner().invoke(
        app,
        [
            "item",
            "bind",
            "--account",
            "acc-1",
            "--item-id",
            "I-1",
            "--spec",
            "颜色=红色",
            "--target",
            "card:7",
            "--quantity",
            "2",
        ],
    )
    listed = CliRunner().invoke(app, ["item", "bindings", "--account", "acc-1", "--item-id", "I-1"])
    removed = CliRunner().invoke(
        app,
        ["item", "unbind", "--account", "acc-1", "--item-id", "I-1", "--spec", "颜色=红色"],
    )
    bad_target = CliRunner().invoke(
        app,
        [
            "item",
            "bind",
            "--account",
            "acc-1",
            "--item-id",
            "I-1",
            "--spec",
            "颜色=红色",
            "--target",
            "card:abc",
        ],
    )
    bad_spec = CliRunner().invoke(
        app,
        [
            "item",
            "bind",
            "--account",
            "acc-1",
            "--item-id",
            "I-1",
            "--spec",
            "颜色",
            "--target",
            "card:7",
        ],
    )

    assert bound.exit_code == 0
    assert "已绑定 颜色:红色 -> card:7" in bound.output
    assert captured["upsert"][0:2] == ("acc-1", "I-1")
    assert listed.exit_code == 0
    assert "card:7" in listed.output
    assert removed.exit_code == 0
    assert "已解除绑定" in removed.output
    assert bad_target.exit_code == 1
    assert "类型:id" in bad_target.output
    assert bad_spec.exit_code == 1
    assert "规格名=规格值" in bad_spec.output


def _binding_for(specs: dict[str, str], *, target: int, quantity: int) -> object:
    return SpecBinding(specs=normalize_specs(specs), target_id=target, quantity=quantity)
