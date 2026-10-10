"""通知渠道的增删改查、绑定与试发送。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from xianyu_agent.application.notify import ChannelSender, NotificationChannelService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.notify import ChannelError

WEBHOOK = "https://hooks.example.com/services/AAA/BBB"


@pytest.fixture
async def notify_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "notify.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _service(handler=None) -> NotificationChannelService:
    if handler is None:
        return NotificationChannelService()
    return NotificationChannelService(
        sender=ChannelSender(transport=httpx.MockTransport(handler), resolve_dns=False)
    )


@pytest.mark.asyncio
async def test_create_rejects_duplicate_name_and_bad_kind(notify_db: None) -> None:
    service = _service()
    await service.create(name="运维", kind="webhook", target=WEBHOOK)

    with pytest.raises(ChannelError, match="已存在"):
        await service.create(name="运维", kind="webhook", target=WEBHOOK)
    with pytest.raises(ChannelError, match="不支持的通知渠道类型"):
        await service.create(name="别的", kind="slack", target=WEBHOOK)


@pytest.mark.asyncio
async def test_update_and_enable_round_trip(notify_db: None) -> None:
    service = _service()
    channel = await service.create(name="运维", kind="webhook", target=WEBHOOK)
    assert channel.local_id is not None

    renamed = await service.update(channel.local_id, name="运维群", events=("account_risk",))
    assert renamed.name == "运维群"
    assert renamed.events == ("account_risk",)

    disabled = await service.set_enabled(channel.local_id, enabled=False)
    assert disabled.enabled is False
    assert await service.list_channels(only_enabled=True) == []


@pytest.mark.asyncio
async def test_update_rejects_invalid_target(notify_db: None) -> None:
    service = _service()
    channel = await service.create(name="运维", kind="webhook", target=WEBHOOK)
    assert channel.local_id is not None

    with pytest.raises(ChannelError, match="只允许 https"):
        await service.update(channel.local_id, target="http://hooks.example.com/x")
    # 校验失败不能把库里那条改坏。
    assert (await service.get(channel.local_id)).target == WEBHOOK


@pytest.mark.asyncio
async def test_binding_requires_existing_account(notify_db: None) -> None:
    service = _service()
    channel = await service.create(name="运维", kind="webhook", target=WEBHOOK)
    assert channel.local_id is not None

    assert await service.bind(channel.local_id, "missing") is False
    assert await service.bind(channel.local_id, "acc-1") is True
    assert await service.bound_accounts(channel.local_id) == ["acc-1"]
    assert await service.unbind(channel.local_id, "acc-1") is True
    assert await service.unbind(channel.local_id, "acc-1") is False
    assert await service.bound_accounts(channel.local_id) == []


@pytest.mark.asyncio
async def test_resolve_ref_accepts_id_and_name(notify_db: None) -> None:
    service = _service()
    channel = await service.create(name="运维", kind="webhook", target=WEBHOOK)
    assert channel.local_id is not None

    assert (await service.resolve_ref("运维")).local_id == channel.local_id
    assert (await service.resolve_ref(str(channel.local_id))).name == "运维"
    with pytest.raises(ValueError, match="找不到通知渠道"):
        await service.resolve_ref("不存在")


@pytest.mark.asyncio
async def test_delete_removes_channel_and_bindings(notify_db: None) -> None:
    service = _service()
    channel = await service.create(name="运维", kind="webhook", target=WEBHOOK)
    assert channel.local_id is not None
    await service.bind(channel.local_id, "acc-1")

    await service.delete(channel.local_id)

    assert await service.list_channels() == []
    with pytest.raises(ValueError, match="不存在"):
        await service.get(channel.local_id)


@pytest.mark.asyncio
async def test_test_send_posts_and_reports_failure(notify_db: None) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    service = _service(handler)
    channel = await service.create(name="运维", kind="webhook", target=WEBHOOK)
    assert channel.local_id is not None

    result = await service.test_send(channel.local_id)

    assert result.ok is True
    assert seen == [WEBHOOK]


@pytest.mark.asyncio
async def test_test_send_reports_http_error_without_leaking_url(notify_db: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    service = _service(handler)
    channel = await service.create(name="运维", kind="webhook", target=WEBHOOK)
    assert channel.local_id is not None

    result = await service.test_send(channel.local_id)

    assert result.ok is False
    assert result.error == "HTTP 500"
    assert WEBHOOK not in (result.error or "")


@pytest.mark.asyncio
async def test_test_send_blocks_private_targets(notify_db: None) -> None:
    """守卫在出站前拦内网地址;渠道配置正常也要拦。"""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        nonlocal called
        called = True
        return httpx.Response(200)

    service = NotificationChannelService(
        sender=ChannelSender(transport=httpx.MockTransport(handler))
    )
    channel = await service.create(name="内网", kind="webhook", target="https://127.0.0.1/hook")
    assert channel.local_id is not None

    result = await service.test_send(channel.local_id)

    assert result.ok is False
    assert result.error == "BLOCKED:outbound_guard"
    assert called is False
