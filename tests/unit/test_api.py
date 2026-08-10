"""Unit tests for the FastAPI layer (source of MCP tools)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi_mcp import FastApiMCP

from xianyu_agent.api.app import app as fastapi_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "api.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def test_account_and_auth_flow(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/accounts", json={"account_id": "acc-1", "remark": "api"})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json()["data"]["account_id"] == "acc-1"

    r = await client.get("/api/v1/accounts")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1

    r = await client.get("/api/v1/accounts/acc-1")
    assert r.status_code == 200
    assert r.json()["data"]["enabled"] is True

    r = await client.post(
        "/api/v1/auth/login",
        json={"account_id": "acc-1", "cookie": "unb=acc-1; _m_h5_tk=seed_1_xyz"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["cookie_saved"] is True

    r = await client.get("/api/v1/auth/status/acc-1")
    assert r.status_code == 200
    assert r.json()["data"]["account_id"] == "acc-1"

    r = await client.put("/api/v1/accounts/acc-1/enabled", json={"enabled": False})
    assert r.status_code == 200

    r = await client.delete("/api/v1/accounts/acc-1")
    assert r.status_code == 200

    # missing account -> 400
    r = await client.get("/api/v1/accounts/nope")
    assert r.status_code == 400


async def test_rules_and_cards(client: httpx.AsyncClient) -> None:
    await client.post("/api/v1/accounts", json={"account_id": "acc-1"})
    r = await client.post(
        "/api/v1/rules",
        json={"name": "k", "type": "keyword", "pattern": "在吗", "reply_text": "在的"},
    )
    assert r.status_code == 200
    rule_id = r.json()["data"]["id"]

    r = await client.post(
        "/api/v1/rules/test", json={"content": "你好,在吗?", "account_id": "acc-1"}
    )
    assert r.status_code == 200
    assert len(r.json()["data"]) >= 1

    r = await client.get("/api/v1/rules")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1

    r = await client.put(f"/api/v1/rules/{rule_id}/enabled", json={"enabled": False})
    assert r.status_code == 200

    r = await client.post(
        "/api/v1/cards",
        json={"account_id": "acc-1", "name": "卡A", "content": "A1\nA2", "type": "text"},
    )
    assert r.status_code == 200
    card_id = r.json()["data"]["id"]
    assert r.json()["data"]["total"] == 2

    r = await client.post(f"/api/v1/cards/{card_id}/consume")
    assert r.status_code == 200
    assert r.json()["data"]["code"] == "A1"

    r = await client.get("/api/v1/cards")
    assert r.status_code == 200
    assert r.json()["data"][0]["remaining"] == 1

    r = await client.post(f"/api/v1/cards/{card_id}/restock", json={"content": "B1"})
    assert r.status_code == 200
    assert r.json()["data"]["remaining"] == 2

    r = await client.delete(f"/api/v1/rules/{rule_id}")
    assert r.status_code == 200


async def test_mcp_mounts_sse_and_pool_status(client: httpx.AsyncClient) -> None:
    mcp = FastApiMCP(fastapi_app, name="xianyu-agent")
    mcp.mount_sse()

    paths = {route.path for route in fastapi_app.routes}
    assert "/sse" in paths
    assert "/sse/messages/" in paths

    r = await client.get("/api/v1/pool/status")
    assert r.status_code == 200
    assert isinstance(r.json()["data"], list)
