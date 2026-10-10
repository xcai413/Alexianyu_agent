"""回复规则配图:文本之后单独发图,失败不重发文本。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, rules as domain_rules
from xianyu_agent.domain.events import MessageReceived
from xianyu_agent.protocol.ws.message_send import build_image_message_request
from xianyu_agent.services.reply_engine import ReplyEngine

runner = CliRunner()
IMAGE = "https://img.example/card.png"


@pytest.fixture
async def rule_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "rule-image.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _event() -> MessageReceived:
    return MessageReceived(
        event_id="e-1",
        account_id="acc-1",
        chat_id="chat-1",
        sender_id="B-1",
        content="发货",
        received_at=datetime.now(UTC),
    )


async def _seed_rule(*, image: str | None = IMAGE) -> None:
    await domain_rules.create_rule(
        "发货说明",
        "keyword",
        "发货",
        "这是你的卡密:",
        account_id="acc-1",
        reply_image_url=image,
    )


@pytest.mark.asyncio
async def test_rule_image_is_sent_after_the_text(rule_db: None) -> None:
    await _seed_rule()
    texts: list[str] = []
    images: list[tuple[str, str, str]] = []

    async def sender(account: str, chat: str, text: str) -> bool:
        texts.append(text)
        return True

    async def image_sender(account: str, chat: str, url: str) -> bool:
        images.append((account, chat, url))
        return True

    engine = ReplyEngine(sender=sender, image_sender=image_sender, mode="RULE_THEN_DEFAULT")
    await engine.handle(_event())

    assert texts == ["这是你的卡密:"]
    assert images == [("acc-1", "chat-1", IMAGE)]


@pytest.mark.asyncio
async def test_image_failure_never_resends_the_text(rule_db: None) -> None:
    await _seed_rule()
    texts: list[str] = []

    async def sender(account: str, chat: str, text: str) -> bool:
        texts.append(text)
        return True

    async def broken_image(account: str, chat: str, url: str) -> bool:
        raise RuntimeError("image boom")

    engine = ReplyEngine(sender=sender, image_sender=broken_image, mode="RULE_THEN_DEFAULT")
    await engine.handle(_event())

    assert texts == ["这是你的卡密:"]


@pytest.mark.asyncio
async def test_rule_without_image_does_not_touch_the_image_port(rule_db: None) -> None:
    await _seed_rule(image=None)
    images: list[str] = []

    async def sender(account: str, chat: str, text: str) -> bool:
        return True

    async def image_sender(account: str, chat: str, url: str) -> bool:
        images.append(url)
        return True

    engine = ReplyEngine(sender=sender, image_sender=image_sender, mode="RULE_THEN_DEFAULT")
    await engine.handle(_event())

    assert images == []


@pytest.mark.asyncio
async def test_text_failure_does_not_attempt_the_image(rule_db: None) -> None:
    await _seed_rule()
    images: list[str] = []

    async def failing_sender(account: str, chat: str, text: str) -> bool:
        return False

    async def image_sender(account: str, chat: str, url: str) -> bool:
        images.append(url)
        return True

    engine = ReplyEngine(
        sender=failing_sender, image_sender=image_sender, mode="RULE_THEN_DEFAULT"
    )
    await engine.handle(_event())

    assert images == []


def test_cli_stores_the_reply_image(rule_db: None) -> None:
    added = runner.invoke(
        app,
        [
            "rule",
            "add",
            "--name",
            "发货说明",
            "--reply",
            "这是你的卡密:",
            "-a",
            "acc-1",
            "--reply-image",
            IMAGE,
        ],
    )

    assert added.exit_code == 0, added.output
    listed = runner.invoke(app, ["rule", "list", "-a", "acc-1"])
    assert listed.exit_code == 0

    async def fetch():
        return await domain_rules.list_rules(account_id="acc-1")

    rows = asyncio.run(fetch())
    assert [row.reply_image_url for row in rows] == [IMAGE]


def test_non_https_image_allows_storing_but_is_rejected_at_send_time(rule_db: None) -> None:
    """规则可以存任意地址(便于先配置),但发送必须在帧构造处被 https 校验拦住。"""
    with pytest.raises(ValueError, match="https"):
        build_image_message_request(
            account_user_id="s", chat_id="c", receiver_id="r", image_url="http://x/a.png"
        )
