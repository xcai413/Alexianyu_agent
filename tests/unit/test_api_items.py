"""API adapter tests for item mirror routes and the detail sync."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from xianyu_agent.api import app as app_module
from xianyu_agent.api.app import app as fastapi_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.item.sku import ItemVariant, normalize_specs
from xianyu_agent.protocol.items_client import RemoteItem
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailFacts

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "api_items.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/v1/accounts", json={"account_id": "acc-1"})
        yield c

    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_item(item_id: str = "I-1") -> int:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id=item_id,
                title="多规格商品",
                price="45",
                status="1",
                detail_url="https://example.invalid/item/I-1",
                main_image_url="https://img.example/main.jpg",
                category_id="123",
                auction_type="a",
                raw_payload=None,
            )
        ],
        synced_at=NOW,
    )
    row = await domain_items.get_item_by_external_id("acc-1", item_id)
    assert row is not None
    return row.id


@pytest.mark.asyncio
async def test_item_list_and_get_expose_specs_and_mirror_fields(client: httpx.AsyncClient) -> None:
    item_pk = await _seed_item()
    await domain_items.replace_platform_variants(
        item_pk,
        [
            ItemVariant(
                specs=normalize_specs({"颜色": "红色"}),
                external_sku_id="SKU-RED",
                price=Decimal("45.00"),
                stock=2,
            )
        ],
    )

    listing = await client.get("/api/v1/items", params={"account_id": "acc-1"})
    detail = await client.get(f"/api/v1/items/{item_pk}")
    missing = await client.get("/api/v1/items/999")

    summary = listing.json()["data"][0]
    assert summary["item_id"] == "I-1"
    assert summary["sold_count"] == 0
    assert summary["has_multi_spec"] is None
    assert "variants" not in summary

    payload = detail.json()["data"]
    assert payload["main_image_url"] == "https://img.example/main.jpg"
    assert payload["images"] == ["https://img.example/main.jpg"]
    assert payload["variants"] == [
        {
            "sku_id": "SKU-RED",
            "specs": [{"name": "颜色", "value": "红色"}],
            "price": "45.00",
            "stock": 2,
            "enabled": True,
        }
    ]
    assert missing.status_code == 400


@pytest.mark.asyncio
async def test_item_sync_route_delegates_and_reports_sold_refresh(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Service:
        async def sync_on_sale(self, account_id: str, *, page_size: int, max_pages: int):
            assert (account_id, page_size, max_pages) == ("acc-1", 5, 2)
            return SimpleNamespace(
                result=SimpleNamespace(
                    account_id=account_id,
                    total=3,
                    created=2,
                    updated=1,
                    marked_off_sale=0,
                ),
                pages=2,
                sold_counts_updated=1,
            )

    monkeypatch.setattr(app_module, "ItemSyncService", _Service)

    response = await client.post(
        "/api/v1/items/sync", json={"account_id": "acc-1", "page_size": 5, "max_pages": 2}
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 3
    assert body["pages"] == 2
    assert body["sold_counts_updated"] == 1


@pytest.mark.asyncio
async def test_item_detail_sync_route_reports_facts(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_item()

    class _Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def sync_facts(self, account_id: str, item_id: str):
            if item_id == "I-MISSING":
                return None
            assert account_id == "acc-1"
            return ItemDetailFacts(item_id=item_id, publisher_id="777", multi_spec=True)

    monkeypatch.setattr(app_module, "ItemDetailService", _Service)

    ok = await client.post(
        "/api/v1/items/detail-sync", json={"account_id": "acc-1", "item_id": "I-1"}
    )
    missing = await client.post(
        "/api/v1/items/detail-sync", json={"account_id": "acc-1", "item_id": "I-MISSING"}
    )

    assert ok.status_code == 200
    assert ok.json()["data"]["multi_spec"] is True
    assert ok.json()["data"]["publisher_identified"] is True
    assert missing.status_code == 400
    assert "不在本地镜像里" in missing.json()["detail"]


@pytest.mark.asyncio
async def test_spec_binding_routes_replace_and_list(client: httpx.AsyncClient) -> None:
    await _seed_item()
    body = {
        "account_id": "acc-1",
        "item_id": "I-1",
        "bindings": [
            {"specs": {"颜色": "红色", "尺码": "M"}, "target_id": 7, "quantity": 2},
            {"specs": {"颜色": "蓝色", "尺码": "M"}, "target_id": 8, "enabled": False},
        ],
    }

    written = await client.put("/api/v1/item-spec-bindings", json=body)
    listed = await client.get(
        "/api/v1/item-spec-bindings", params={"account_id": "acc-1", "item_id": "I-1"}
    )
    cleared = await client.put(
        "/api/v1/item-spec-bindings",
        json={"account_id": "acc-1", "item_id": "I-1", "bindings": []},
    )
    bad_target = await client.put(
        "/api/v1/item-spec-bindings",
        json={
            "account_id": "acc-1",
            "item_id": "I-1",
            "bindings": [{"specs": {"颜色": "红色"}, "target_id": 7, "target_kind": "warehouse"}],
        },
    )

    assert written.status_code == 200
    assert written.json()["data"]["written"] == 2
    rows = listed.json()["data"]
    assert {row["target_id"] for row in rows} == {7, 8}
    assert next(row for row in rows if row["target_id"] == 7)["specs"] == {
        "颜色": "红色",
        "尺码": "M",
    }
    assert next(row for row in rows if row["target_id"] == 8)["enabled"] is False
    assert cleared.json()["data"]["bindings"] == []
    assert bad_target.status_code == 400
    assert "未知库存来源类型" in bad_target.json()["detail"]
