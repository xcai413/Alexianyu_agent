"""Unit tests for publish drafts, batch state and the table importers (M13)."""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from xianyu_agent.application.item.importers import (
    parse_csv,
    parse_tsv,
    parse_xlsx,
    parse_zip_bundle,
)
from xianyu_agent.domain.item.publish_job import (
    INTERRUPTED_ERROR,
    BatchPublishEntry,
    BatchPublishJob,
    PublishDraft,
    preview_drafts,
    validate_draft,
)
from xianyu_agent.domain.item.sku import ItemVariant, normalize_specs

XMLNS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)

CSV_TEXT = (
    "标题,价格,库存,图片,描述\n"
    "帆布包,45,3,https://img.example/a.jpg;https://img.example/b.jpg,全新\n"
    ",50,1,https://img.example/c.jpg,缺标题\n"
    "水杯,29.5,1,,带盖\n"
)


def _draft(title: str = "帆布包", **overrides: object) -> PublishDraft:
    values: dict[str, object] = {
        "account_id": "acc-1",
        "title": title,
        "price": Decimal("45"),
        "quantity": 2,
        "source_row": 2,
    }
    values.update(overrides)
    return PublishDraft(**values)  # type: ignore[arg-type]


def _job(*entries: BatchPublishEntry, state: str = "pending") -> BatchPublishJob:
    return BatchPublishJob(
        id="batch-1",
        account_id="acc-1",
        entries=entries or (BatchPublishEntry(index=0, draft=_draft()),),
        state=state,  # type: ignore[arg-type]
        created_at=NOW,
    )


def _column_letter(index: int) -> str:
    return chr(ord("A") + index)


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    shared: list[str] = [cell for row in rows for cell in row if cell and not _is_number(cell)]
    shared = list(dict.fromkeys(shared))
    sheet_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells = [
            _numeric_cell(_column_letter(index), row_number, value)
            if _is_number(value)
            else _shared_cell(_column_letter(index), row_number, shared.index(value))
            for index, value in enumerate(row)
            if value
        ]
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    sheet = (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{XMLNS}">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>"
    )
    strings = (
        f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{XMLNS}" count="{len(shared)}">'
        + "".join(f"<si><t>{value}</t></si>" for value in shared)
        + "</sst>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        archive.writestr("xl/sharedStrings.xml", strings)
    return buffer.getvalue()


def _is_number(value: str) -> bool:
    try:
        Decimal(value)
    except Exception:
        return False
    return True


def _numeric_cell(column: str, row: int, value: str) -> str:
    return f'<c r="{column}{row}"><v>{value}</v></c>'


def _shared_cell(column: str, row: int, index: int) -> str:
    return f'<c r="{column}{row}" t="s"><v>{index}</v></c>'


def test_draft_rejects_impossible_values() -> None:
    with pytest.raises(ValueError, match="title"):
        _draft(title="   ")
    with pytest.raises(ValueError, match="quantity"):
        _draft(quantity=0)
    with pytest.raises(ValueError, match="price"):
        _draft(price=Decimal("-1"))


def test_preview_blocks_missing_price_and_duplicate_rows() -> None:
    preview = preview_drafts(
        (
            _draft(),
            _draft(source_row=3),
            _draft(title="水杯", price=None, source_row=4),
        )
    )

    assert preview.total == 3
    assert preview.ready == 1
    assert preview.blocked == 2
    assert [problem.code for problem in preview.rows[1].problems] == ["duplicate_row"]
    assert [problem.code for problem in preview.rows[2].problems] == ["price_required"]


def test_validate_draft_flags_fixed_postage_and_duplicate_specs() -> None:
    variant = ItemVariant(specs=normalize_specs({"颜色": "红色"}), stock=1)
    problems = validate_draft(
        _draft(
            postage_mode="fixed",
            postage=None,
            original_price=Decimal("10"),
            variants=(variant, variant),
        )
    )

    assert {problem.code for problem in problems} == {
        "postage_required",
        "original_price_invalid",
        "variant_duplicate",
    }


def test_batch_progress_and_state_transitions() -> None:
    job = _job(
        BatchPublishEntry(index=0, draft=_draft()),
        BatchPublishEntry(index=1, draft=_draft(source_row=3)),
    )

    assert job.progress.pending == 2
    assert job.next_pending().index == 0

    started = job.start()
    assert started.state == "running"
    assert started.started_at is not None

    done = started.with_entry(
        BatchPublishEntry(index=0, draft=_draft(), state="succeeded", external_item_id="I-9")
    )
    assert done.progress.succeeded == 1
    assert done.progress.pending == 1
    assert done.state == "running"

    finished = done.with_entry(
        BatchPublishEntry(index=1, draft=_draft(source_row=3), state="skipped")
    )
    assert finished.progress.finished is True
    assert finished.state == "completed"
    assert finished.finished_at is not None


def test_cancelled_batch_stops_handing_out_rows() -> None:
    job = _job(state="cancelled")

    assert job.next_pending() is None
    assert job.with_entry(replace(job.entries[0], state="succeeded")).state == "cancelled"


def test_recovery_flags_interrupted_rows_instead_of_republishing() -> None:
    job = _job(
        BatchPublishEntry(index=0, draft=_draft(), state="succeeded", external_item_id="I-1"),
        BatchPublishEntry(index=1, draft=_draft(source_row=3), state="running"),
        BatchPublishEntry(index=2, draft=_draft(source_row=4), state="pending"),
        state="running",
    )

    recovered = job.recover_after_restart()

    assert recovered.state == "pending"
    assert recovered.progress.succeeded == 1
    assert recovered.progress.pending == 1
    assert recovered.progress.failed == 1
    interrupted = recovered.entries[1]
    assert interrupted.error == INTERRUPTED_ERROR
    assert recovered.next_pending().index == 2


def test_csv_import_maps_headers_parses_values_and_rejects_titleless_rows() -> None:
    result = parse_csv(CSV_TEXT, account_id="acc-1")

    assert len(result.drafts) == 2
    assert [row.row_number for row in result.rejected] == [3]
    assert result.rejected[0].reason == "缺少标题"
    first = result.drafts[0]
    assert first.title == "帆布包"
    assert first.price == Decimal("45.00")
    assert first.quantity == 3
    assert first.images == ("https://img.example/a.jpg", "https://img.example/b.jpg")
    assert first.description == "全新"
    assert result.ignored_headers == ()
    assert parse_csv("标题,未知列\n包,1\n", account_id="acc-1").ignored_headers == ("未知列",)


def test_tsv_import_reads_tab_separated_rows() -> None:
    result = parse_tsv("标题\t价格\n帆布包\t45\n", account_id="acc-1")

    assert [draft.title for draft in result.drafts] == ["帆布包"]
    assert result.drafts[0].price == Decimal("45.00")


def test_xlsx_import_reads_shared_strings_and_numbers() -> None:
    data = _xlsx_bytes(
        [
            ["标题", "价格", "库存"],
            ["帆布包", "45", "3"],
            ["水杯", "29.5", "1"],
        ]
    )

    result = parse_xlsx(data, account_id="acc-1")

    assert [draft.title for draft in result.drafts] == ["帆布包", "水杯"]
    assert result.drafts[1].price == Decimal("29.50")
    assert result.drafts[0].quantity == 3
    with pytest.raises(ValueError, match="工作表"):
        parse_xlsx(data, account_id="acc-1", sheet_index=3)


def test_zip_bundle_reads_the_table_and_reports_images() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("batch/rows.csv", CSV_TEXT)
        archive.writestr("batch/images/a.jpg", b"jpg")
        archive.writestr("__MACOSX/._rows.csv", b"x")
        archive.writestr("images/b.png", b"png")

    result = parse_zip_bundle(buffer.getvalue(), account_id="acc-1")

    assert len(result.drafts) == 2
    assert result.bundled_images == ("a.jpg", "b.png")


def test_zip_bundle_rejects_ambiguous_or_empty_archives() -> None:
    with pytest.raises(ValueError, match="多个表格"):
        parse_zip_bundle(_zip({"a.csv": b"title\n", "b.tsv": b"title\n"}), account_id="acc-1")
    with pytest.raises(ValueError, match="没有找到"):
        parse_zip_bundle(_zip({"a.jpg": b"jpg"}), account_id="acc-1")


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()
