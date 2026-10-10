"""API adapter tests for batch import and item lifecycle (source of MCP tools)."""

from __future__ import annotations

import base64
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.api.app import app as fastapi_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import items as domain_items
from xianyu_agent.infrastructure.database.models.item import Item
from xianyu_agent.protocol.items_client import RemoteItem

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
CSV_TEXT = "标题,价格,库存\n帆布包,45,3\n,50,1\n"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "api_batch.db"))
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


async def _create_batch(client: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    payload: dict[str, object] = {
        "account_id": "acc-1",
        "filename": "rows.csv",
        "content": CSV_TEXT,
    }
    payload.update(overrides)
    return await client.post("/api/v1/item-batches", json=payload)


async def _seed_old_item(days: int = 30) -> None:
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id="I-OLD",
                title="帆布包",
                price="45",
                status="1",
                detail_url=None,
                main_image_url=None,
                category_id="123",
                auction_type="a",
                raw_payload=None,
            )
        ],
    )
    async with db_mod.get_async_session() as session:
        row = (await session.execute(select(Item).where(Item.item_id == "I-OLD"))).scalar_one()
        row.first_seen_at = datetime.now(UTC) - timedelta(days=days)
        await session.commit()


@pytest.mark.asyncio
async def test_batch_create_returns_preview_and_rejected_rows(client: httpx.AsyncClient) -> None:
    response = await _create_batch(client)

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["job"]["id"]
    assert body["job"]["state"] == "pending"
    assert (body["preview"]["total"], body["preview"]["ready"], body["preview"]["blocked"]) == (
        1,
        1,
        0,
    )
    assert body["rejected"] == [{"row_number": 3, "reason": "缺少标题"}]
    assert body["job"]["entries"][0]["title"] == "帆布包"


@pytest.mark.asyncio
async def test_batch_create_rejects_bad_input(client: httpx.AsyncClient) -> None:
    unsupported = await _create_batch(client, filename="rows.pdf")
    missing_content = await _create_batch(client, content=None)
    header_only = await _create_batch(client, content="标题,价格\n")
    bad_base64 = await _create_batch(
        client, filename="rows.xlsx", content=None, content_base64="@@@"
    )
    base64_text = await _create_batch(
        client, filename="rows.csv", content=None, content_base64=base64.b64encode(b"x").decode()
    )

    assert unsupported.status_code == 400
    assert "不支持的表格格式" in unsupported.json()["detail"]
    assert missing_content.status_code == 400
    assert "必须提供 content" in missing_content.json()["detail"]
    assert header_only.status_code == 400
    assert "没有可导入的草稿行" in header_only.json()["detail"]
    assert bad_base64.status_code == 400
    assert "base64" in bad_base64.json()["detail"]
    assert base64_text.status_code == 400
    assert "不支持 base64 传参" in base64_text.json()["detail"]


@pytest.mark.asyncio
async def test_batch_create_accepts_a_base64_zip_bundle(client: httpx.AsyncClient) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("rows.csv", CSV_TEXT)
        archive.writestr("images/a.jpg", b"jpg")

    response = await _create_batch(
        client,
        filename="bundle.zip",
        content=None,
        content_base64=base64.b64encode(buffer.getvalue()).decode(),
    )

    assert response.status_code == 200
    assert response.json()["data"]["bundled_images"] == ["a.jpg"]


@pytest.mark.asyncio
async def test_batch_list_get_recover_and_cancel(client: httpx.AsyncClient) -> None:
    job_id = (await _create_batch(client)).json()["data"]["job"]["id"]

    listing = await client.get("/api/v1/item-batches", params={"account_id": "acc-1"})
    fetched = await client.get(f"/api/v1/item-batches/{job_id}")
    missing = await client.get("/api/v1/item-batches/missing")
    recovered = await client.post(f"/api/v1/item-batches/{job_id}/recover")
    cancelled = await client.post(f"/api/v1/item-batches/{job_id}/cancel")

    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()["data"]] == [job_id]
    assert "entries" not in listing.json()["data"][0]
    assert fetched.json()["data"]["entries"][0]["state"] == "pending"
    assert missing.status_code == 400
    assert recovered.json()["data"]["state"] == "pending"
    assert cancelled.json()["data"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_lifecycle_plan_lists_decisions(client: httpx.AsyncClient) -> None:
    await _seed_old_item()

    response = await client.get(
        "/api/v1/item-lifecycle/plan",
        params={"account_id": "acc-1", "delist_after_days": 10},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["counts"] == {"KEEP": 0, "DELIST": 1, "DELETE": 0}
    assert body["rows"][0]["item_id"] == "I-OLD"
    assert "上架 30 天" in body["rows"][0]["reason"]


@pytest.mark.asyncio
async def test_lifecycle_apply_needs_confirmation_and_an_executor(
    client: httpx.AsyncClient,
) -> None:
    await _seed_old_item()
    payload = {"account_id": "acc-1", "delist_after_days": 10}

    unconfirmed = await client.post("/api/v1/item-lifecycle/apply", json=payload)
    confirmed = await client.post("/api/v1/item-lifecycle/apply", json={**payload, "confirm": True})

    assert unconfirmed.status_code == 400
    assert "未确认执行" in unconfirmed.json()["detail"]
    assert confirmed.status_code == 400
    assert "未配置商品动作执行器" in confirmed.json()["detail"]


@pytest.mark.asyncio
async def test_batch_run_needs_confirmation_and_never_fakes_success(
    client: httpx.AsyncClient,
) -> None:
    created = await _create_batch(
        client,
        content="标题,价格,图片\n帆布包,45,/nonexistent/a.jpg\n",
    )
    job_id = created.json()["data"]["job"]["id"]

    unconfirmed = await client.post(f"/api/v1/item-batches/{job_id}/run", json={})
    confirmed = await client.post(f"/api/v1/item-batches/{job_id}/run", json={"confirm": True})

    assert unconfirmed.status_code == 400
    assert "未确认执行" in unconfirmed.json()["detail"]
    assert confirmed.status_code == 200
    body = confirmed.json()["data"]
    assert (body["succeeded"], body["failed"]) == (0, 1)
    assert "不是可读的本地文件" in body["job"]["entries"][0]["error"]


@pytest.mark.asyncio
async def test_batch_retry_requeues_a_failed_entry(client: httpx.AsyncClient) -> None:
    created = await _create_batch(
        client,
        content="标题,价格,图片\n帆布包,45,/nonexistent/a.jpg\n",
    )
    job_id = created.json()["data"]["job"]["id"]
    await client.post(f"/api/v1/item-batches/{job_id}/run", json={"confirm": True})

    retried = await client.post(f"/api/v1/item-batches/{job_id}/entries/0/retry")
    again = await client.post(f"/api/v1/item-batches/{job_id}/entries/0/retry")

    assert retried.status_code == 200
    entry = retried.json()["data"]["entries"][0]
    assert entry["state"] == "pending"
    assert "不是可读的本地文件" in entry["error"]
    assert retried.json()["data"]["finished_at"] is None
    assert again.status_code == 400
    assert "只能重试失败或跳过" in again.json()["detail"]
