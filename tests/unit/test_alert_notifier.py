"""运维告警 webhook:默认关闭、出站守卫、按事项幂等。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from xianyu_agent.application.attention import AlertNotifier, AttentionItem
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AlertDispatch, database as db_mod

WEBHOOK = "https://alerts.example.com/hook"


@pytest.fixture
async def alert_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "alerts.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _item(kind: str = "automation_run", target: str = "#7 polish item-1") -> AttentionItem:
    return AttentionItem(
        kind=kind,
        target=target,
        state="uncertain",
        reason="transport outcome unknown",
        next_command="xianyu-agent automation resolve --run-id 7",
        account_id="acc-1",
        since=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_disabled_when_no_url_is_configured(alert_db: None) -> None:
    notifier = AlertNotifier()

    result = await notifier.dispatch((_item(),))

    assert notifier.enabled is False
    assert result.disabled is True
    assert result.sent == 0
    async with db_mod.async_session_factory() as session:
        assert list((await session.scalars(select(AlertDispatch))).all()) == []


@pytest.mark.asyncio
async def test_each_item_is_sent_only_once(alert_db: None) -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": request.content.decode()})
        return httpx.Response(202)

    notifier = AlertNotifier(
        url=WEBHOOK,
        transport=httpx.MockTransport(handler),
        resolve_dns=False,
    )

    first = await notifier.dispatch((_item(),))
    second = await notifier.dispatch((_item(),))
    other = await notifier.dispatch((_item(target="#8 polish item-2"),))

    assert (first.sent, first.skipped, first.failed) == (1, 0, 0)
    # 同一事项第二次只跳过,不再发请求;新事项仍然发得出去。
    assert (second.sent, second.skipped) == (0, 1)
    assert (other.sent, other.skipped) == (1, 0)
    assert len(calls) == 2
    assert "resolve --run-id 7" in calls[0]["body"]
    async with db_mod.async_session_factory() as session:
        rows = list((await session.scalars(select(AlertDispatch))).all())
    assert len(rows) == 2
    assert all(row.ok for row in rows)


@pytest.mark.asyncio
async def test_webhook_failure_is_recorded_but_never_raised(alert_db: None) -> None:
    notifier = AlertNotifier(
        url=WEBHOOK,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        resolve_dns=False,
    )

    result = await notifier.dispatch((_item(),))

    assert (result.sent, result.failed) == (0, 1)
    async with db_mod.async_session_factory() as session:
        row = (await session.scalars(select(AlertDispatch))).one()
    assert row.ok is False
    assert row.error == "HTTP 500"


@pytest.mark.asyncio
async def test_outbound_guard_blocks_a_private_webhook(alert_db: None) -> None:
    notifier = AlertNotifier(url="https://127.0.0.1/hook", resolve_dns=False)

    result = await notifier.dispatch((_item(),))

    assert result.failed == 1
    async with db_mod.async_session_factory() as session:
        # 被拦截时不留发送事实:它没有发出去,不该占用幂等键。
        assert list((await session.scalars(select(AlertDispatch))).all()) == []


@pytest.mark.asyncio
async def test_transport_error_is_classified(alert_db: None) -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    notifier = AlertNotifier(
        url=WEBHOOK, transport=httpx.MockTransport(explode), resolve_dns=False
    )

    result = await notifier.dispatch((_item(),))

    assert result.failed == 1
    async with db_mod.async_session_factory() as session:
        row = (await session.scalars(select(AlertDispatch))).one()
    assert row.error == "ConnectError"
