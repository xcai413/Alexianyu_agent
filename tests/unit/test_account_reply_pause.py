"""账号级自动回复暂停:到点自动恢复,并优先于会话状态。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.events import MessageReceived
from xianyu_agent.infrastructure.database.models import Account as AccountRow
from xianyu_agent.services.reply_engine import ReplyEngine

runner = CliRunner()


@pytest.fixture
async def pause_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "pause.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def test_default_account_is_not_paused(pause_db: None) -> None:
    assert await domain_accounts.is_reply_paused("acc-1") is False
    assert await domain_accounts.reply_pause_until("acc-1") is None


async def test_pause_expires_without_manual_cleanup(pause_db: None) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    await domain_accounts.set_reply_pause(
        "acc-1", until=now + timedelta(minutes=30), reason="人工处理风控"
    )

    assert await domain_accounts.is_reply_paused("acc-1", now=now) is True
    # 到期后自动恢复:不需要人工清理,避免留下一个永远停着的账号。
    assert (
        await domain_accounts.is_reply_paused("acc-1", now=now + timedelta(minutes=31))
        is False
    )
    record = await domain_accounts.get_account("acc-1")
    assert record is not None
    assert record.reply_paused_reason == "人工处理风控"


async def test_resume_clears_the_pause_and_reason(pause_db: None) -> None:
    await domain_accounts.set_reply_pause(
        "acc-1", until=datetime.now(UTC) + timedelta(hours=1), reason="手动暂停"
    )
    ok = await domain_accounts.set_reply_pause("acc-1", until=None)

    assert ok is True
    assert await domain_accounts.is_reply_paused("acc-1") is False
    record = await domain_accounts.get_account("acc-1")
    assert record is not None
    assert record.reply_paused_until is None
    assert record.reply_paused_reason is None


async def test_pause_reports_missing_account(pause_db: None) -> None:
    assert await domain_accounts.set_reply_pause("missing", until=None) is False


def test_cli_pauses_and_resumes(pause_db: None) -> None:
    paused = runner.invoke(
        app,
        ["account", "pause-reply", "-i", "acc-1", "-m", "30", "--reason", "人工处理"],
    )
    assert paused.exit_code == 0, paused.output
    assert "已暂停 30 分钟" in paused.output

    shown = runner.invoke(app, ["account", "show", "-i", "acc-1"])
    assert shown.exit_code == 0
    assert "人工处理" in shown.output

    resumed = runner.invoke(app, ["account", "resume-reply", "-i", "acc-1"])
    assert resumed.exit_code == 0
    assert "已恢复" in resumed.output


def test_cli_pause_rejects_unknown_account(pause_db: None) -> None:
    result = runner.invoke(app, ["account", "pause-reply", "-i", "missing", "-m", "5"])

    assert result.exit_code == 1
    assert "不存在" in result.output


async def test_reply_engine_skips_all_conversations_while_paused(pause_db: None) -> None:
    """账号暂停时,即使会话本身是开启的,也不应产生回复。"""
    sent: list[str] = []

    async def sender(_account: str, _chat: str, text: str) -> bool:
        sent.append(text)
        return True

    await domain_accounts.set_reply_pause(
        "acc-1", until=datetime.now(UTC) + timedelta(minutes=10), reason="风控"
    )
    engine = ReplyEngine(sender=sender, mode="RULE_ONLY")
    event = MessageReceived(
        event_id="e-1",
        account_id="acc-1",
        chat_id="chat-1",
        sender_id="B-1",
        content="在吗",
        received_at=datetime.now(UTC),
    )

    await engine.handle(event, message_id=None)

    assert sent == []


async def test_pause_uses_utc_for_naive_stored_values(pause_db: None) -> None:
    """SQLite 回读的是无时区值,比较前必须按 UTC 归一化,否则会 TypeError。"""
    async with get_async_session() as session, session.begin():
        await session.execute(
            update(AccountRow)
            .where(AccountRow.account_id == "acc-1")
            .values(reply_paused_until=datetime(2099, 1, 1, 0, 0))  # 故意写 naive 值
        )

    until = await domain_accounts.reply_pause_until("acc-1")

    assert until is not None
    assert until.tzinfo is UTC
    assert await domain_accounts.is_reply_paused("acc-1") is True
