"""外部 API 卡密源:GET 取卡、试调用与卡券导入。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.application.delivery import ApiConfigService
from xianyu_agent.application.inventory import InventoryImportService, InventoryService
from xianyu_agent.application.ports.secret_vault import SecretValue
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod, get_async_session
from xianyu_agent.domain.delivery import ApiDeliveryConfig
from xianyu_agent.infrastructure.database.models import DeliveryJob as DeliveryJobRow
from xianyu_agent.infrastructure.delivery import ApiDeliveryProvider

CARD_API = "https://cards.example/api/issue"


@pytest.fixture
async def card_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "cards.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def test_get_config_body_is_treated_as_query_parameters() -> None:
    config = ApiDeliveryConfig(
        name="get-api",
        url=CARD_API,
        method="GET",
        body='{"orderId": "{{order_id}}", "count": 1, "flag": true, "note": null}',
        response_path="data.card",
    )

    assert config.body_is_query is True
    assert config.method == "GET"


@pytest.mark.asyncio
@respx.mock
async def test_get_provider_sends_query_parameters_and_stays_off_the_body() -> None:
    route = respx.get(url__startswith=CARD_API).mock(
        return_value=httpx.Response(200, json={"data": {"card": "CARD-XYZ"}})
    )
    config = ApiDeliveryConfig(
        name="get-api",
        url=CARD_API,
        method="GET",
        headers={"X-Token": "{{secret:card_token}}"},
        body='{"orderId": "{{order_id}}", "count": 1, "flag": true}',
        response_path="data.card",
    )
    provider = ApiDeliveryProvider(
        vault=_Vault({"card_token": "tok-1"}), resolve_dns=False
    )

    result = await provider.fetch(config, {"order_id": "O-9", "quantity": "1"})

    request = route.calls.last.request
    assert result.content == "CARD-XYZ"
    assert result.dispatched is True
    assert request.method == "GET"
    assert request.content == b""
    assert request.url.params["orderId"] == "O-9"
    assert request.url.params["count"] == "1"
    assert request.url.params["flag"] == "true"
    assert request.headers["X-Token"] == "tok-1"


class _Vault:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def resolve(self, ref):
        return SecretValue(self._values[ref.value])


@pytest.mark.asyncio
@respx.mock
async def test_config_service_trial_call_returns_card_without_touching_orders(
    card_db: None,
) -> None:
    await ApiConfigService().create(
        name="trial-api",
        url=CARD_API,
        method="GET",
        body='{"orderId": "{{order_id}}"}',
        response_path="card",
    )
    respx.get(url__startswith=CARD_API).mock(
        return_value=httpx.Response(200, json={"card": "SECRET-CARD-1234"})
    )

    result = await ApiConfigService().test_fetch(
        "trial-api", provider=ApiDeliveryProvider(resolve_dns=False)
    )

    assert result.content == "SECRET-CARD-1234"
    assert result.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_config_service_trial_call_reports_classified_failure(card_db: None) -> None:
    await ApiConfigService().create(
        name="trial-api",
        url=CARD_API,
        method="POST",
        body='{"orderId": "{{order_id}}"}',
        response_path="card",
    )
    respx.post(url__startswith=CARD_API).mock(return_value=httpx.Response(500))

    result = await ApiConfigService().test_fetch(
        "trial-api", provider=ApiDeliveryProvider(resolve_dns=False)
    )

    assert result.content is None
    assert result.dispatched is True
    assert "HTTP 500" in (result.error or "")


@pytest.mark.asyncio
async def test_trial_call_never_creates_a_delivery_job(card_db: None) -> None:
    await ApiConfigService().create(
        name="trial-api",
        url=CARD_API,
        method="POST",
        body="{}",
        response_path="card",
    )
    with respx.mock:
        respx.post(url__startswith=CARD_API).mock(
            return_value=httpx.Response(200, json={"card": "C-1"})
        )
        await ApiConfigService().test_fetch(
            "trial-api", provider=ApiDeliveryProvider(resolve_dns=False)
        )

    async with get_async_session() as session:
        jobs = list((await session.scalars(select(DeliveryJobRow))).all())
    assert jobs == []


def test_card_unit_import_is_idempotent_for_duplicate_lines(card_db: Path) -> None:
    async def scenario() -> None:
        pool = await InventoryService().create_pool(name="卡券池", type_="data")
        first = await InventoryImportService().import_units(
            pool.local_id or 0, ["CARD-1", "CARD-2", "", "CARD-1"]
        )
        second = await InventoryImportService().import_units(pool.local_id or 0, ["CARD-2"])
        return first, second

    first, second = asyncio.run(scenario())
    assert (first.added, first.skipped_duplicates, first.skipped_blank) == (2, 1, 1)
    assert (second.added, second.skipped_duplicates) == (0, 1)
