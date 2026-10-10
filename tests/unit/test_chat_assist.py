"""快捷回复与买家备注:范围隔离、幂等与人工边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xianyu_agent.application.chat import ChatAssistService
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts

runner = CliRunner()


@pytest.fixture
async def chat_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "chat.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def test_quick_reply_rejects_empty_and_duplicate_titles(chat_db: None) -> None:
    service = ChatAssistService()

    with pytest.raises(ValueError, match="标题不能为空"):
        await service.add_quick_reply(title="  ", content="在的")
    with pytest.raises(ValueError, match="内容不能为空"):
        await service.add_quick_reply(title="问候", content="  ")

    await service.add_quick_reply(title="问候", content="在的")
    with pytest.raises(ValueError, match="已存在"):
        await service.add_quick_reply(title="问候", content="在的")


async def test_account_view_sees_its_own_and_global_templates(chat_db: None) -> None:
    service = ChatAssistService()
    await service.add_quick_reply(title="全局问候", content="在的")
    await service.add_quick_reply(title="主号专用", content="主号话术", account_id="acc-1")
    await service.add_quick_reply(title="二号专用", content="二号话术", account_id="acc-2")

    first = await service.list_quick_replies(account_id="acc-1")

    assert {row.title for row in first} == {"全局问候", "主号专用"}
    assert [row.title for row in await service.list_quick_replies()] == ["全局问候"]


async def test_quick_reply_for_unknown_account_is_rejected(chat_db: None) -> None:
    with pytest.raises(ValueError, match="账号 missing 不存在"):
        await ChatAssistService().add_quick_reply(
            title="X", content="Y", account_id="missing"
        )


async def test_quick_reply_delete_reports_missing(chat_db: None) -> None:
    service = ChatAssistService()
    row = await service.add_quick_reply(title="问候", content="在的")

    assert await service.delete_quick_reply(row.id) is True
    assert await service.delete_quick_reply(row.id) is False


async def test_quick_phrase_metadata_update_and_usage(chat_db: None) -> None:
    service = ChatAssistService()
    row = await service.add_quick_reply(
        title="售后", content="请提供订单号", category="售后", sort_order=2
    )
    assert (row.category, row.enabled, row.use_count) == ("售后", True, 0)
    updated = await service.update_quick_reply(row.id, category="常用", enabled=False)
    assert updated is not None
    assert (updated.category, updated.enabled) == ("常用", False)
    assert await service.list_quick_replies(include_disabled=False) == []
    assert len(await service.list_quick_replies(include_disabled=True)) == 1
    assert await service.use_quick_reply(row.id) is True
    listed = await service.list_quick_replies()
    assert listed[0].use_count == 1
    assert await service.use_quick_reply(99999) is False


async def test_buyer_note_is_upserted_per_account(chat_db: None) -> None:
    service = ChatAssistService()

    first = await service.save_buyer_note(
        account_id="acc-1", buyer_id="B-1", note="老客户,注意态度"
    )
    updated = await service.save_buyer_note(
        account_id="acc-1", buyer_id="B-1", note="老客户,已发货两次"
    )
    other = await service.save_buyer_note(
        account_id="acc-2", buyer_id="B-1", note="另一个账号下的同名买家"
    )

    assert first.note == "老客户,注意态度"
    assert updated.note == "老客户,已发货两次"
    # 同一个 buyer_id 在不同账号下是两条独立备注,不能串号。
    assert other.account_id == "acc-2"
    assert (await service.get_buyer_note(account_id="acc-1", buyer_id="B-1")).note == (
        "老客户,已发货两次"
    )
    assert await service.get_buyer_note(account_id="acc-1", buyer_id="NOPE") is None


async def test_buyer_note_requires_content_and_delete_reports_missing(chat_db: None) -> None:
    service = ChatAssistService()

    with pytest.raises(ValueError, match="备注内容不能为空"):
        await service.save_buyer_note(account_id="acc-1", buyer_id="B-1", note="   ")
    with pytest.raises(ValueError, match="buyer_id 不能为空"):
        await service.save_buyer_note(account_id="acc-1", buyer_id="  ", note="x")

    await service.save_buyer_note(account_id="acc-1", buyer_id="B-1", note="记录")
    assert await service.delete_buyer_note(account_id="acc-1", buyer_id="B-1") is True
    assert await service.delete_buyer_note(account_id="acc-1", buyer_id="B-1") is False


async def test_message_filters_support_batch_update_toggle_and_scope(chat_db: None) -> None:
    service = ChatAssistService()

    created, skipped = await service.batch_create_message_filters(
        account_id="acc-1", keywords=["退款", "退款", "站外"], filter_type="skip_reply"
    )
    assert (created, skipped) == (2, 1)
    rows = await service.list_message_filters(account_id="acc-1")
    assert {row.keyword for row in rows} == {"退款", "站外"}
    assert await service.list_message_filters(account_id="acc-2") == []

    updated = await service.update_message_filter(rows[0].id, keyword="改名", enabled=False)
    assert updated is not None
    assert (updated.keyword, updated.enabled) == ("改名", False)
    assert await service.set_message_filter_enabled(rows[0].id, True) is True
    assert await service.delete_message_filter(rows[0].id) is True
    assert await service.batch_delete_message_filters([rows[1].id, rows[1].id, 99999]) == 1


def test_cli_manages_message_filter_batch(chat_db: None) -> None:
    result = runner.invoke(
        app,
        [
            "message", "filter", "batch-add", "-a", "acc-1",
            "-k", "退款", "-k", "站外",
        ],
    )
    assert result.exit_code == 0, result.output
    listed = runner.invoke(app, ["message", "filter", "list", "-a", "acc-1"])
    assert listed.exit_code == 0
    assert "退款" in listed.output
    assert "站外" in listed.output


def test_cli_manages_quick_replies_and_notes(chat_db: None) -> None:
    added = runner.invoke(
        app, ["message", "quick-reply", "add", "--title", "问候", "--content", "在的"]
    )
    assert added.exit_code == 0, added.output
    listed = runner.invoke(app, ["message", "quick-reply", "list", "-a", "acc-1"])
    assert listed.exit_code == 0
    assert "问候" in listed.output

    noted = runner.invoke(
        app,
        ["message", "note", "set", "-a", "acc-1", "--buyer-id", "B-1", "--note", "定金已付"],
    )
    assert noted.exit_code == 0, noted.output
    shown = runner.invoke(
        app, ["message", "note", "show", "-a", "acc-1", "--buyer-id", "B-1"]
    )
    assert shown.exit_code == 0
    assert "定金已付" in shown.output


def test_cli_delete_requires_confirmation(chat_db: None) -> None:
    async def seed() -> int:
        row = await ChatAssistService().add_quick_reply(title="问候", content="在的")
        return row.id

    reply_id = asyncio.run(seed())
    denied = runner.invoke(app, ["message", "quick-reply", "delete", str(reply_id)])

    assert denied.exit_code == 1
    assert "请加 --yes" in denied.output
