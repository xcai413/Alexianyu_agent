"""已读处理:本地计数清零与平台上报帧。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, messages as domain_messages
from xianyu_agent.infrastructure.database.models import (
    Account as AccountRow,
    Conversation as ConversationRow,
    Message as MessageRow,
)
from xianyu_agent.protocol.ws.message_read import (
    READ_LWP,
    MessageReadNotSent,
    MessageReadRejected,
    build_read_frame,
    report_read,
)

runner = CliRunner()


@pytest.fixture
async def read_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "read.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_conversation(unread: int = 3) -> None:
    async with get_async_session() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == "acc-1").limit(1)
            )
        ).scalar_one()
        conversation = ConversationRow(
            account_id=account.id,
            external_conversation_id="chat-1",
            buyer_id="B-1",
            unread_count=unread,
        )
        session.add(conversation)
        await session.flush()
        for index in range(2):
            session.add(
                MessageRow(
                    account_id=account.id,
                    conversation_id=conversation.id,
                    message_id=f"pnm-{index}",
                    chat_id="chat-1",
                    direction="inbound",
                    content_type="text",
                    content="你好",
                    received_at=datetime.now(UTC),
                )
            )


def test_read_frame_matches_the_reference_shape() -> None:
    frame = build_read_frame(["pnm-1", "pnm-2"], mid_factory=lambda: "mid-1")

    assert frame["lwp"] == READ_LWP
    assert frame["headers"] == {"mid": "mid-1"}
    # 参考实现按 [payload, [ids]] 的形状发参数。
    assert frame["body"] == [{}, [["pnm-1", "pnm-2"]]]


def test_read_frame_rejects_an_empty_id_list() -> None:
    with pytest.raises(ValueError, match="至少需要一个"):
        build_read_frame(["  ", ""])


@pytest.mark.asyncio
async def test_report_read_classifies_failures() -> None:
    class _Router:
        def __init__(self, *, code: int | None = 200) -> None:
            self._code = code
            self.cancelled: list[str] = []

        def register(self, _request_id: str):
            return object()

        def cancel(self, request_id: str) -> None:
            self.cancelled.append(request_id)

        async def wait(self, _pending, *, timeout_s=None):
            return {"code": self._code}

    async def refused(_frame) -> bool:
        return False

    async def wrote(_frame) -> bool:
        return True

    router = _Router()
    with pytest.raises(MessageReadNotSent):
        await report_read(router, refused, ["pnm-1"])  # type: ignore[arg-type]
    assert router.cancelled

    with pytest.raises(MessageReadRejected, match="code=403"):
        await report_read(_Router(code=403), wrote, ["pnm-1"])  # type: ignore[arg-type]

    payload = await report_read(_Router(), wrote, ["pnm-1"])  # type: ignore[arg-type]
    assert payload["code"] == 200


@pytest.mark.asyncio
async def test_local_mark_read_clears_the_counter(read_db: None) -> None:
    await _seed_conversation(unread=3)

    previous = await domain_messages.mark_conversation_read(
        account_id="acc-1", chat_id="chat-1"
    )

    assert previous == 3
    conversation = await domain_messages.get_conversation(
        account_id="acc-1", chat_id="chat-1"
    )
    assert conversation is not None
    assert conversation.unread_count == 0
    # 未知会话要如实返回 None,而不是假装清过了。
    assert (
        await domain_messages.mark_conversation_read(account_id="acc-1", chat_id="nope")
        is None
    )


@pytest.mark.asyncio
async def test_list_unread_messages_returns_platform_ids(read_db: None) -> None:
    await _seed_conversation()

    rows = await domain_messages.list_unread_messages(
        account_id="acc-1", chat_id="chat-1"
    )

    assert sorted(row.message_id for row in rows) == ["pnm-0", "pnm-1"]


@pytest.mark.asyncio
async def test_unread_list_is_bounded_and_reports_truncation(read_db: None) -> None:
    await _seed_conversation()

    bounded = await domain_messages.list_unread_messages(
        account_id="acc-1", chat_id="chat-1", limit=1
    )

    assert len(bounded) == 1
    assert (
        await domain_messages.has_more_inbound_messages_with_ids(
            account_id="acc-1", chat_id="chat-1", limit=1
        )
        is True
    )
    assert (
        await domain_messages.has_more_inbound_messages_with_ids(
            account_id="acc-1", chat_id="chat-1", limit=10
        )
        is False
    )


def test_cli_mark_read_is_local_only(read_db: None) -> None:
    asyncio.run(_seed_conversation(unread=5))

    result = runner.invoke(app, ["message", "mark-read", "-a", "acc-1", "-c", "chat-1"])

    assert result.exit_code == 0, result.output
    assert "原为 5 条" in result.output
    assert "report-read" in result.output


def test_cli_report_read_requires_confirmation(read_db: None) -> None:
    asyncio.run(_seed_conversation())

    denied = runner.invoke(
        app, ["message", "report-read", "-a", "acc-1", "-c", "chat-1"]
    )

    assert denied.exit_code == 1
    assert "请加 --yes" in denied.output
