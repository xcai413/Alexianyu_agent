"""API 池发货:配置维护与执行链路(Issue #5 Phase 6, 6H)。"""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.application.delivery import (
    ApiConfigService,
    DeliveryExecutor,
    DeliveryPolicyService,
    DeliveryRequest,
    SendOutcome,
)
from xianyu_agent.application.inventory import InventoryService
from xianyu_agent.application.ports.secret_vault import SecretValue
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.delivery import ApiConfigError, DeliveryJobState
from xianyu_agent.domain.transitions import InvalidStateTransition
from xianyu_agent.infrastructure.database.models import Account as AccountRow, Order as OrderRow
from xianyu_agent.infrastructure.delivery import ApiDeliveryProvider
from xianyu_agent.infrastructure.delivery.api_provider import MAX_RESPONSE_BYTES
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault

runner = CliRunner()
API_URL = "https://cards.example.com/dispatch"


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "api_flow.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(db_mod.init_db())
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


async def _seed_order(order_id: str = "O-1") -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    async with db_mod.async_session_factory() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == "acc-1").limit(1)
            )
        ).scalar_one()
        session.add(
            OrderRow(
                account_id=account.id,
                order_id=order_id,
                item_id="I-1",
                item_title="多规格商品",
                buyer_id="B-1",
                buyer_name="买家",
                quantity=1,
                status="paid",
                amount=45.0,
            )
        )


async def _seed_api_pool(config_name: str) -> int:
    pool = await InventoryService().create_pool(
        name="发卡池", type_="api", api_config_ref=config_name
    )
    return pool.local_id


def _sender_collecting(sent: list[str]):
    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent.append(text)
        return SendOutcome.success(external_message_id="msg-1")

    return sender


async def test_config_service_roundtrip_and_validation(wired_db: Path) -> None:
    service = ApiConfigService()

    created = await service.create(name="发卡", url=API_URL, body='{"order": "{{order_id}}"}')
    assert created.local_id is not None
    assert [config.name for config in await service.list_configs()] == ["发卡"]

    assert (await service.resolve_ref("发卡")).local_id == created.local_id
    assert (await service.resolve_ref(str(created.local_id))).local_id == created.local_id

    updated = await service.update(created.local_id, response_path="data.card", timeout_seconds=5)
    assert updated.response_path == "data.card"
    assert updated.timeout_seconds == 5

    with pytest.raises(ApiConfigError, match="已存在"):
        await service.create(name="发卡", url=API_URL)
    with pytest.raises(ValueError, match="找不到发货 API 配置"):
        await service.resolve_ref("不存在")

    disabled = await service.set_enabled(created.local_id, enabled=False)
    assert disabled.enabled is False
    await service.delete(created.local_id)
    assert await service.list_configs() == []


async def test_api_pool_delivery_completes_without_local_stock(wired_db: Path) -> None:
    await _seed_order()
    await ApiConfigService().create(name="发卡", url=API_URL, response_path="data.card")
    pool_id = await _seed_api_pool("发卡")
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")
    sent: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"card": "API-CODE-9"}})

    executor = DeliveryExecutor(
        sender=_sender_collecting(sent),
        api_provider=ApiDeliveryProvider(transport=httpx.MockTransport(handler), resolve_dns=False),
    )
    result = await executor.execute(DeliveryRequest(account_id="acc-1", order_id="O-1"))

    assert result.decision == "allow"
    assert result.job.state == DeliveryJobState.COMPLETED.value
    assert sent == ["API-CODE-9"]
    assert result.reservation_id is None
    async with db_mod.async_session_factory() as session:
        order = (await session.execute(select(OrderRow))).scalar_one()
    assert order.status == "delivered"
    assert (
        await DeliveryExecutor().plan(DeliveryRequest(account_id="acc-1", order_id="O-1"))
    ).allowed is False  # 已发货的订单不会再走一遍


async def test_default_executor_resolves_api_secret_from_encrypted_vault(
    wired_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_order()
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("XIANYU_SECRET_MASTER_KEY", key)
    vault = SqlAlchemySecretVault(FernetKeyring.from_environment())
    ref = await vault.put(SecretValue("Bearer secret-value"))
    await ApiConfigService().create(
        name="带密钥发卡",
        url=API_URL,
        headers={"Authorization": "{{secret:" + ref.value + "}}"},
        response_path="data.card",
    )
    pool_id = await _seed_api_pool("带密钥发卡")
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-value"
        return httpx.Response(200, json={"data": {"card": "API-CODE-SECRET"}})

    provider = ApiDeliveryProvider.from_environment()
    provider._transport = httpx.MockTransport(handler)
    provider._resolve_dns = False
    monkeypatch.setattr(ApiDeliveryProvider, "from_environment", lambda: provider)

    result = await DeliveryExecutor(sender=_sender_collecting(sent)).execute(
        DeliveryRequest(account_id="acc-1", order_id="O-1")
    )

    assert result.job.state == DeliveryJobState.COMPLETED.value
    assert sent == ["API-CODE-SECRET"]


async def test_api_fetch_not_dispatched_is_retryable(wired_db: Path) -> None:
    await _seed_order()
    await ApiConfigService().create(name="发卡", url=API_URL)
    pool_id = await _seed_api_pool("发卡")
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    executor = DeliveryExecutor(
        sender=_sender_collecting([]),
        api_provider=ApiDeliveryProvider(transport=httpx.MockTransport(handler), resolve_dns=False),
    )
    result = await executor.execute(DeliveryRequest(account_id="acc-1", order_id="O-1"))

    assert result.job.state == DeliveryJobState.FAILED_RETRYABLE.value
    assert "建连失败" in result.reason


async def test_api_fetch_unknown_outcome_freezes_job(wired_db: Path) -> None:
    await _seed_order()
    await ApiConfigService().create(name="发卡", url=API_URL)
    pool_id = await _seed_api_pool("发卡")
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    executor = DeliveryExecutor(
        sender=_sender_collecting([]),
        api_provider=ApiDeliveryProvider(transport=httpx.MockTransport(handler), resolve_dns=False),
    )
    result = await executor.execute(DeliveryRequest(account_id="acc-1", order_id="O-1"))

    assert result.job.state == DeliveryJobState.UNCERTAIN.value
    assert result.decision == "require_confirmation"
    assert "可能已经发卡" in result.reason


@pytest.mark.parametrize("compressed", [False, True])
async def test_oversize_api_response_freezes_job_without_send_or_refetch(
    wired_db: Path, compressed: bool
) -> None:
    await _seed_order()
    await ApiConfigService().create(name="发卡", url=API_URL)
    pool_id = await _seed_api_pool("发卡")
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")
    payload = b'{"card":"CODE-1"}' + b" " * MAX_RESPONSE_BYTES
    if compressed:
        payload = gzip.compress(payload)
    calls: list[httpx.Request] = []
    sent: list[str] = []

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield payload

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            stream=ResponseStream(),
            headers={"content-encoding": "gzip"} if compressed else {},
        )

    provider = ApiDeliveryProvider(transport=httpx.MockTransport(handler), resolve_dns=False)
    request = DeliveryRequest(account_id="acc-1", order_id="O-1")
    result = await DeliveryExecutor(sender=_sender_collecting(sent), api_provider=provider).execute(
        request
    )
    with pytest.raises(InvalidStateTransition, match="uncertain"):
        await DeliveryExecutor(sender=_sender_collecting(sent), api_provider=provider).execute(
            request
        )

    assert result.job.state == DeliveryJobState.UNCERTAIN.value
    assert result.decision == "require_confirmation"
    assert "超过" in result.reason
    assert len(calls) == 1
    assert sent == []
    async with db_mod.async_session_factory() as session:
        order = (await session.execute(select(OrderRow))).scalar_one()
    assert order.status == "paid"


async def test_api_pool_without_config_blocks(wired_db: Path) -> None:
    await _seed_order()
    pool_id = await _seed_api_pool("还没配的接口")
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")

    result = await DeliveryExecutor(sender=_sender_collecting([])).execute(
        DeliveryRequest(account_id="acc-1", order_id="O-1")
    )

    assert result.decision == "block"
    assert "找不到发货 API 配置" in result.reason


def test_cli_api_config_flow(wired_db: Path) -> None:
    created = runner.invoke(
        app,
        [
            "delivery",
            "api",
            "set",
            "--name",
            "发卡",
            "--url",
            API_URL,
            "--body",
            '{"order": "{{order_id}}"}',
            "--header",
            "Authorization=Bearer {{secret:card_api_key}}",
        ],
    )
    assert created.exit_code == 0, created.output
    assert "发卡" in created.output
    assert "card_api_key" in created.output

    listed = runner.invoke(app, ["delivery", "api", "list"])
    assert listed.exit_code == 0
    assert "发卡" in listed.output
    assert "card_api_key" in listed.output

    invalid = runner.invoke(
        app, ["delivery", "api", "set", "--name", "坏配置", "--url", "http://cards.example.com/x"]
    )
    assert invalid.exit_code == 1
    assert "https" in invalid.output

    disabled = runner.invoke(app, ["delivery", "api", "disable", "1"])
    assert disabled.exit_code == 0
    assert "已停用" in disabled.output

    removed = runner.invoke(app, ["delivery", "api", "delete", "1", "--yes"])
    assert removed.exit_code == 0
    assert "已删除" in removed.output


def test_cli_api_secret_lifecycle_never_echoes_plaintext(
    wired_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XIANYU_SECRET_MASTER_KEY", Fernet.generate_key().decode())
    plaintext = "delivery-secret-value"
    created = runner.invoke(
        app,
        ["delivery", "api", "secret", "add", "--label", "供应商 A"],
        input=f"{plaintext}\n",
    )
    assert created.exit_code == 0, created.output
    assert plaintext not in created.output
    marker = "{{secret:"
    ref = created.output.split(marker, 1)[1].split("}}", 1)[0]

    status = runner.invoke(app, ["delivery", "api", "secret", "status", ref])
    assert status.exit_code == 0
    assert "configured=Y" in status.output

    replacement = "rotated-delivery-secret"
    rotated = runner.invoke(
        app,
        ["delivery", "api", "secret", "rotate", ref],
        input=f"{replacement}\n",
    )
    assert rotated.exit_code == 0, rotated.output
    assert replacement not in rotated.output
