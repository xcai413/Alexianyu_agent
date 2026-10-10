"""Tests for the risk gate in front of real platform calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.item import (
    AccountCoolingError,
    ItemCategoryService,
    ItemDetailService,
    ItemPublishService,
    ItemSyncService,
)
from xianyu_agent.application.item.platform_guard import AccountRiskGate
from xianyu_agent.application.pacing import AccountPacer
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.account import risk as account_risk
from xianyu_agent.domain.item.publish_job import PublishDraft
from xianyu_agent.protocol.items_client import ItemSnapshot, ItemSyncError, RemoteItem
from xianyu_agent.protocol.mtop.endpoints.item_category import ItemCategoryError
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailError
from xianyu_agent.protocol.mtop.endpoints.item_publish import UploadedImage

USER_VALIDATE = "FAIL_SYS_USER_VALIDATE, RGV587_ERROR::SM::哎哟喂,被挤爆啦"


class _VirtualClock:
    """Clock advanced by the pacer's own sleeps, so pacing tests do not really wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _paced_gate(
    rate_per_minute: int, *, jitter_ratio: float = 0.0
) -> tuple[AccountRiskGate, _VirtualClock]:
    clock = _VirtualClock()
    pacer = AccountPacer(
        rate_per_minute=rate_per_minute,
        jitter_ratio=jitter_ratio,
        clock=clock,
        sleep=clock.sleep,
        random_source=lambda: 1.0,
    )
    return AccountRiskGate(pacer=pacer), clock


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "guard.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


class _Client:
    """Counts calls and raises a configurable error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error
        self.uploads = 0

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def fetch_all_on_sale(self, _account_id: str, *, page_size: int, max_pages: int):
        self.calls += 1
        self._raise()
        return ItemSnapshot(items=[], fetched_at=datetime.now(UTC), pages=0)

    async def recommend_category(self, _account_id: str, **_kwargs: object):
        self.calls += 1
        self._raise()

    async def fetch_item_detail(self, _account_id: str, _item_id: str):
        self.calls += 1
        self._raise()

    async def upload_image(self, *args: object, **kwargs: object) -> UploadedImage:
        self.uploads += 1
        self._raise()
        return UploadedImage(url="https://img.example/a.jpg")

    async def publish_item(self, _account_id: str, _request: object):
        self.calls += 1
        self._raise()


async def _seed_item() -> None:
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id="I-1",
                title="多规格商品",
                price="45",
                status="1",
                detail_url=None,
                main_image_url=None,
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
        ],
    )


def _draft() -> PublishDraft:
    return PublishDraft(
        account_id="acc-1",
        title="帆布包",
        price=Decimal("45"),
        quantity=1,
        images=("https://img.example/a.jpg",),
        source_row=2,
    )


@pytest.mark.asyncio
async def test_user_validate_failure_opens_the_circuit_and_blocks_the_next_call(clean_db) -> None:
    failing = _Client(ItemSyncError(f"账号 acc-1 {USER_VALIDATE}"))

    with pytest.raises(ItemSyncError):
        await ItemSyncService(lambda: failing).sync_on_sale("acc-1")

    circuit = await account_risk.get("acc-1")
    assert circuit is not None
    assert circuit.is_cooling() is True
    assert failing.calls == 1

    healthy = _Client()
    with pytest.raises(AccountCoolingError, match="冷却"):
        await ItemSyncService(lambda: healthy).sync_on_sale("acc-1")

    assert healthy.calls == 0
    async with db_mod.get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == "acc-1"))
        ).scalar_one()
    assert account.desired_state == "stopped"


@pytest.mark.asyncio
async def test_ordinary_failures_do_not_open_the_circuit(clean_db) -> None:
    failing = _Client(ItemSyncError("商品列表网络请求失败"))

    with pytest.raises(ItemSyncError):
        await ItemSyncService(lambda: failing).sync_on_sale("acc-1")

    assert await account_risk.get("acc-1") is None
    healthy = _Client()
    await ItemSyncService(lambda: healthy).sync_on_sale("acc-1")
    assert healthy.calls == 1


@pytest.mark.asyncio
async def test_every_platform_entry_is_gated_while_cooling(clean_db) -> None:
    await _seed_item()
    await account_risk.open_user_validate("acc-1")
    category_client = _Client()
    detail_client = _Client()
    writer = _Client()

    with pytest.raises(AccountCoolingError):
        await ItemCategoryService(category_client).recommend("acc-1", keyword="帆布包")
    with pytest.raises(AccountCoolingError):
        await ItemDetailService(detail_client=detail_client).sync_facts("acc-1", "I-1")
    with pytest.raises(AccountCoolingError):
        await ItemDetailService(detail_client=detail_client).fetch_structure("acc-1", "I-1")
    with pytest.raises(AccountCoolingError):
        await ItemDetailService(detail_client=detail_client).fetch_facts("acc-1", "I-1")
    with pytest.raises(AccountCoolingError):
        await ItemPublishService(writer).publish(_draft())

    assert category_client.calls == 0
    assert detail_client.calls == 0
    assert writer.calls == 0
    assert writer.uploads == 0


@pytest.mark.asyncio
async def test_expired_cooldown_still_requires_manual_recovery(clean_db) -> None:
    await account_risk.open_user_validate("acc-1", now=datetime.now(UTC) - timedelta(minutes=30))
    client = _Client()

    with pytest.raises(AccountCoolingError, match="熔断尚未解除"):
        await ItemSyncService(lambda: client).sync_on_sale("acc-1")

    assert client.calls == 0


@pytest.mark.asyncio
async def test_category_failure_with_the_validate_code_opens_the_circuit(clean_db) -> None:
    client = _Client(ItemCategoryError(USER_VALIDATE))

    with pytest.raises(ItemCategoryError):
        await ItemCategoryService(client).recommend("acc-1", keyword="帆布包")

    assert await account_risk.get("acc-1") is not None


@pytest.mark.asyncio
async def test_detail_failure_with_the_validate_code_opens_the_circuit(clean_db) -> None:
    await _seed_item()
    client = _Client(ItemDetailError(USER_VALIDATE))

    with pytest.raises(ItemDetailError):
        await ItemDetailService(detail_client=client).sync_facts("acc-1", "I-1")

    assert await account_risk.get("acc-1") is not None


@pytest.mark.asyncio
async def test_gate_paces_calls_beyond_the_account_quota(clean_db) -> None:
    gate, clock = _paced_gate(rate_per_minute=60)

    for _ in range(60):
        await gate.ensure_ready("acc-1")
    assert clock.slept == []

    await gate.ensure_ready("acc-1")

    assert clock.slept == pytest.approx([1.0])


@pytest.mark.asyncio
async def test_gate_uses_a_bucket_per_account(clean_db) -> None:
    gate, clock = _paced_gate(rate_per_minute=60)
    for _ in range(61):
        await gate.ensure_ready("acc-1")

    await gate.ensure_ready("acc-2")

    assert len(clock.slept) == 1


@pytest.mark.asyncio
async def test_blocked_call_does_not_consume_a_pacing_token(clean_db) -> None:
    gate, clock = _paced_gate(rate_per_minute=1)
    await gate.ensure_ready("acc-1")
    await account_risk.open_user_validate("acc-1")

    with pytest.raises(AccountCoolingError):
        await gate.ensure_ready("acc-1")

    assert clock.slept == []


@pytest.mark.asyncio
async def test_pacing_applies_through_the_sync_entry_point(clean_db) -> None:
    gate, clock = _paced_gate(rate_per_minute=1)
    client = _Client()
    service = ItemSyncService(lambda: client, risk_gate=gate)

    await service.sync_on_sale("acc-1")
    await service.sync_on_sale("acc-1")

    assert client.calls == 2
    assert clock.slept == pytest.approx([60.0])
