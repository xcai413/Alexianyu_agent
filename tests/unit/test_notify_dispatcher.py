"""多渠道派发:订阅过滤、账号绑定、按渠道幂等、失败不抛。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from xianyu_agent.application.attention import AttentionItem
from xianyu_agent.application.notify import (
    ChannelSender,
    NotificationChannelService,
    NotificationDispatcher,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AlertDispatch, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts


@pytest.fixture
async def notify_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "dispatch.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_accounts.create_account("acc-2", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _item(account_id: str | None = "acc-1", kind: str = "delivery_job") -> AttentionItem:
    return AttentionItem(
        kind=kind,
        target="order-1",
        state="uncertain",
        reason="结果不明",
        next_command="xianyu-agent attention list",
        account_id=account_id,
        since=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
    )


def _dispatcher(handler, calls: list[str]):
    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    return NotificationDispatcher(
        sender=ChannelSender(transport=httpx.MockTransport(wrapped), resolve_dns=False)
    )


@pytest.mark.asyncio
async def test_no_channels_is_a_noop(notify_db: None) -> None:
    calls: list[str] = []
    result = await _dispatcher(lambda request: httpx.Response(200), calls).dispatch((_item(),))

    assert result.disabled is True
    assert calls == []
    async with db_mod.async_session_factory() as session:
        assert list((await session.scalars(select(AlertDispatch))).all()) == []


@pytest.mark.asyncio
async def test_each_item_is_sent_once_per_channel(notify_db: None) -> None:
    calls: list[str] = []
    service = NotificationChannelService()
    await service.create(name="A", kind="webhook", target="https://a.example.com/hook")
    await service.create(name="B", kind="webhook", target="https://b.example.com/hook")
    dispatcher = _dispatcher(lambda request: httpx.Response(200), calls)

    first = await dispatcher.dispatch((_item(),))
    second = await dispatcher.dispatch((_item(),))

    assert first.sent == 2
    assert second.sent == 0
    assert second.skipped == 2
    assert sorted(calls) == ["https://a.example.com/hook", "https://b.example.com/hook"]


@pytest.mark.asyncio
async def test_event_subscription_filters_channels(notify_db: None) -> None:
    calls: list[str] = []
    await NotificationChannelService().create(
        name="只看风控",
        kind="webhook",
        target="https://risk.example.com/hook",
        events=("account_risk",),
    )
    dispatcher = _dispatcher(lambda request: httpx.Response(200), calls)

    result = await dispatcher.dispatch((_item(kind="delivery_job"),))

    assert result.sent == 0
    assert result.skipped == 1
    assert calls == []


@pytest.mark.asyncio
async def test_account_binding_limits_delivery(notify_db: None) -> None:
    calls: list[str] = []
    service = NotificationChannelService()
    channel = await service.create(
        name="主号专用", kind="webhook", target="https://one.example.com/hook"
    )
    assert channel.local_id is not None
    assert await service.bind(channel.local_id, "acc-2") is True
    dispatcher = _dispatcher(lambda request: httpx.Response(200), calls)

    wrong_account = await dispatcher.dispatch((_item(account_id="acc-1"),))
    right_account = await dispatcher.dispatch((_item(account_id="acc-2"),))

    assert wrong_account.sent == 0
    assert right_account.sent == 1
    assert calls == ["https://one.example.com/hook"]


@pytest.mark.asyncio
async def test_failed_send_is_recorded_and_not_retried(notify_db: None) -> None:
    calls: list[str] = []
    await NotificationChannelService().create(
        name="坏渠道", kind="webhook", target="https://broken.example.com/hook"
    )
    dispatcher = _dispatcher(lambda request: httpx.Response(503), calls)

    first = await dispatcher.dispatch((_item(),))
    second = await dispatcher.dispatch((_item(),))

    assert first.failed == 1
    # 结果已记一条事实,不会自动重发;要重发必须换掉事项状态或人工处理。
    assert second.sent == 0
    assert second.skipped == 1
    async with db_mod.async_session_factory() as session:
        rows = list((await session.scalars(select(AlertDispatch))).all())
    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].error == "HTTP 503"
    assert rows[0].channel_id is not None


@pytest.mark.asyncio
async def test_transport_error_does_not_raise(notify_db: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    await NotificationChannelService().create(
        name="断线", kind="webhook", target="https://down.example.com/hook"
    )
    dispatcher = _dispatcher(handler, [])

    result = await dispatcher.dispatch((_item(),))

    assert result.failed == 1


@pytest.mark.asyncio
async def test_blocked_target_counts_as_failure_without_request(notify_db: None) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        nonlocal called
        called = True
        return httpx.Response(200)

    service = NotificationChannelService()
    await service.create(name="内网", kind="webhook", target="https://10.0.0.5/hook")
    dispatcher = NotificationDispatcher(
        sender=ChannelSender(transport=httpx.MockTransport(handler))
    )

    result = await dispatcher.dispatch((_item(),))

    assert result.failed == 1
    assert called is False
    async with db_mod.async_session_factory() as session:
        rows = list((await session.scalars(select(AlertDispatch))).all())
    assert rows[0].error == "BLOCKED:outbound_guard"
