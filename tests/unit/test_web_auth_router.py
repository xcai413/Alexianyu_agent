from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from xianyu_agent.application.automation import PlatformAutomationService
from xianyu_agent.application.delivery import ApiConfigService, DeliveryJobService
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryReservationService,
    InventoryService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, AuditLog, Conversation, database as db_mod, get_async_session
from xianyu_agent.domain import items as domain_items, rules as domain_rules
from xianyu_agent.domain.account import state as domain_accounts
from xianyu_agent.domain.delivery import DeliveryJobState, FailureKind
from xianyu_agent.domain.item.sku import ItemVariant, SpecValue
from xianyu_agent.domain.order import orders as domain_orders
from xianyu_agent.infrastructure.database.models import (
    Conversation as ConversationRow,
    Message as MessageRow,
)
from xianyu_agent.infrastructure.delivery import ApiDeliveryProvider
from xianyu_agent.interfaces.web.auth.router import router
from xianyu_agent.interfaces.web.auth.security import hash_password
from xianyu_agent.interfaces.web.dashboard.router import router as dashboard_router
from xianyu_agent.interfaces.web.operations import router as operations_module
from xianyu_agent.interfaces.web.operations.router import router as operations_router
from xianyu_agent.protocol.items_client import RemoteItem
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailError, ItemDetailFacts
from xianyu_agent.protocol.mtop.endpoints.order_read import XianyuOrderReadClient
from xianyu_agent.protocol.mtop.endpoints.user_profile import UserProfile, UserProfileError
from xianyu_agent.protocol.orders_client import RemoteSoldOrder

CARD_API_URL = "https://cards.example.com/issue"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "web-auth.db"))
    monkeypatch.setenv(
        "XIANYU_WEB_ADMIN_PASSWORD_HASH", hash_password("correct horse battery staple")
    )
    monkeypatch.setenv("XIANYU_WEB_SESSION_SECRET", "unit-test-signing-secret")
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    app = FastAPI()
    app.include_router(router)
    app.include_router(dashboard_router)
    app.include_router(operations_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    reset_settings_cache()


async def test_login_session_and_csrf_logout(client: httpx.AsyncClient) -> None:
    unauthenticated = await client.post("/api/v1/web/auth/logout")
    assert unauthenticated.status_code == 401
    logged_in = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    assert logged_in.status_code == 200
    assert "HttpOnly" in logged_in.headers["set-cookie"]
    csrf = logged_in.json()["data"]["csrf_token"]
    assert (await client.get("/api/v1/web/auth/session")).json()["data"]["authenticated"] is True
    rejected = await client.post("/api/v1/web/auth/logout")
    assert rejected.status_code == 403
    logged_out = await client.post("/api/v1/web/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logged_out.status_code == 200


async def test_logout_revokes_copied_session_on_admin_endpoints(client: httpx.AsyncClient) -> None:
    logged_in = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    token = client.cookies.get("xianyu_web_session")
    csrf = logged_in.json()["data"]["csrf_token"]
    copied_headers = {"Cookie": f"xianyu_web_session={token}", "X-CSRF-Token": csrf}
    logged_out = await client.post("/api/v1/web/auth/logout", headers=copied_headers)
    assert logged_out.status_code == 200
    assert (await client.get("/api/v1/dashboard/summary", headers=copied_headers)).status_code == 401
    assert (await client.get("/api/v1/web/auth/session", headers=copied_headers)).json() == {
        "data": {"authenticated": False}
    }
    assert (await client.post("/api/v1/web/auth/logout", headers=copied_headers)).status_code == 401
    denied_write = await client.post(
        "/api/v1/web/operations/backup/import",
        json={"document": {"schema_version": 1}},
        headers=copied_headers,
    )
    assert denied_write.status_code == 401


async def test_login_rejects_bad_password(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/web/auth/login", json={"password": "wrong password"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_CREDENTIALS"


async def test_dashboard_requires_admin_session(client: httpx.AsyncClient) -> None:
    denied = await client.get("/api/v1/dashboard/summary")
    assert denied.status_code == 401
    assert denied.json()["detail"]["code"] == "AUTH_REQUIRED"

    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    summary = await client.get("/api/v1/dashboard/summary")
    assert summary.status_code == 200
    assert summary.json()["data"] == {
        "accounts": 0,
        "online_accounts": 0,
        "inbound_messages": 0,
        "orders": 0,
        "on_sale_items": 0,
    }


async def test_operations_require_admin_and_do_not_expose_control_fields(
    client: httpx.AsyncClient,
) -> None:
    denied = await client.get("/api/v1/web/operations/accounts")
    assert denied.status_code == 401

    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    accounts = await client.get("/api/v1/web/operations/accounts")
    runtime = await client.get("/api/v1/web/operations/runtime")

    assert accounts.status_code == 200
    assert accounts.json()["data"] == []
    assert runtime.status_code == 200
    assert runtime.json()["data"] == []


async def test_backup_web_export_and_import_use_admin_csrf_and_dry_run(
    client: httpx.AsyncClient,
) -> None:
    denied = await client.get("/api/v1/web/operations/backup/export")
    assert denied.status_code == 401

    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    exported = await client.get("/api/v1/web/operations/backup/export")
    assert exported.status_code == 200, exported.text
    document = exported.json()["data"]
    assert document["schema_version"] == 1
    assert document["includes_cards"] is False

    payload = {
        "schema_version": 1,
        "accounts": [{"account_id": "dry-run-account"}],
    }
    denied_import = await client.post(
        "/api/v1/web/operations/backup/import", json={"document": payload}
    )
    assert denied_import.status_code == 403

    imported = await client.post(
        "/api/v1/web/operations/backup/import",
        json={"document": payload},
        headers={"X-CSRF-Token": csrf},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["data"]["mode"] == "dry_run"
    assert imported.json()["data"]["total_created"] == 1
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == "dry-run-account"))
        ).scalar_one_or_none()
        audit = (
            await session.execute(select(AuditLog).where(AuditLog.action == "backup.imported"))
        ).scalar_one()
    assert account is None
    assert audit.result == "success"


async def test_notification_channel_has_explicit_web_update_route(
    client: httpx.AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    created = await client.post(
        "/api/v1/web/operations/notify/channels",
        json={
            "name": "消息通知",
            "kind": "webhook",
            "target": "https://notify.example.com/first",
            "events": ["message_received"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    channel_id = created.json()["data"]["id"]

    updated = await client.put(
        f"/api/v1/web/operations/notify/channels/{channel_id}",
        json={
            "target": "https://notify.example.com/second",
            "events": ["message_received", "account_risk"],
            "enabled": False,
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["data"] == {
        "id": channel_id,
        "name": "消息通知",
        "kind": "webhook",
        "enabled": False,
        "events": ["message_received", "account_risk"],
    }


async def test_local_item_mutations_require_csrf_and_do_not_call_platform(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_items.apply_on_sale_snapshot(
        "acc-1", [RemoteItem("I-1", "商品一", "9.9", "1", None, None, None, None, {"id": "I-1"})]
    )
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    denied = await client.patch("/api/v1/web/operations/items/acc-1/I-1", json={"title": "新标题"})
    assert denied.status_code == 403
    updated = await client.patch(
        "/api/v1/web/operations/items/acc-1/I-1",
        json={"title": "新标题", "stock": 2},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    archived = await client.delete(
        "/api/v1/web/operations/items/acc-1/I-1",
        headers={"X-CSRF-Token": csrf},
    )
    assert archived.status_code == 200, archived.text


async def test_mirrored_business_data_requires_admin_and_stays_read_only(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    denied = await client.get("/api/v1/web/operations/messages", params={"account_id": "acc-1"})
    assert denied.status_code == 401

    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    for resource in ("messages", "orders", "items", "inventory", "rules", "audit"):
        response = await client.get(
            f"/api/v1/web/operations/{resource}", params={"account_id": "acc-1"}
        )
        assert response.status_code == 200
        assert response.json()["data"] == []


async def test_order_api_exposes_synced_title_in_correct_account(client: httpx.AsyncClient) -> None:
    for account_id, title in (("acc-1", "订单商品标题"), ("acc-2", "另一个账号的同ID商品")):
        await domain_accounts.create_account(account_id)
        await domain_orders.apply_sold_orders_snapshot(
            account_id,
            [
                RemoteSoldOrder(
                    order_id="O-title",
                    item_id="I-title",
                    item_title=title,
                    buyer_id=f"buyer-{account_id}",
                    buyer_name=f"买家-{account_id}",
                    amount=9.9,
                    quantity=1,
                    status="paid",
                    placed_at=datetime.now(UTC),
                    raw_payload={},
                )
            ],
        )
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    for account_id, title in (("acc-1", "订单商品标题"), ("acc-2", "另一个账号的同ID商品")):
        response = await client.get(
            "/api/v1/web/operations/orders", params={"account_id": account_id}
        )
        assert response.status_code == 200
        assert len(response.json()["data"]) == 1
        assert response.json()["data"][0]["item_title"] == title
        assert response.json()["data"][0]["buyer_id"] == f"buyer-{account_id}"
        assert response.json()["data"][0]["buyer_name"] == f"买家-{account_id}"
        assert "buyer_phone" not in response.json()["data"][0]
        assert "buyer_address" not in response.json()["data"][0]
        assert "raw_payload" not in response.json()["data"][0]


async def test_order_api_keeps_missing_buyer_identity_null(client: httpx.AsyncClient) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_orders.apply_sold_orders_snapshot(
        "acc-1",
        [
            RemoteSoldOrder(
                order_id="O-missing-buyer",
                item_id="I-title",
                item_title="商品",
                buyer_id=None,
                buyer_name=None,
                amount=9.9,
                quantity=1,
                status="paid",
                placed_at=None,
                raw_payload={},
            )
        ],
    )
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    response = await client.get("/api/v1/web/operations/orders", params={"account_id": "acc-1"})
    assert response.status_code == 200
    assert response.json()["data"][0]["buyer_id"] is None
    assert response.json()["data"][0]["buyer_name"] is None


async def test_account_command_requires_csrf_and_only_queues_work(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})

    denied = await client.post(
        "/api/v1/web/operations/accounts/acc-1/commands", json={"action": "start"}
    )
    assert denied.status_code == 403

    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    queued = await client.post(
        "/api/v1/web/operations/accounts/acc-1/commands",
        json={"action": "start"},
        headers={"X-CSRF-Token": csrf},
    )

    assert queued.status_code == 200
    assert queued.json()["data"]["action"] == "start"
    assert queued.json()["data"]["status"] == "pending"
    command_id = queued.json()["data"]["command_id"]
    status = await client.get(f"/api/v1/web/operations/commands/{command_id}")
    assert status.status_code == 200
    assert status.json()["data"]["status"] == "pending"
    async with get_async_session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "worker_command_queued")
            )
        ).scalar_one()
    assert audit.actor == "web"
    assert audit.target == "acc-1"


async def test_conversation_takeover_requires_csrf_and_is_audited(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    unknown = await client.get(
        "/api/v1/web/operations/conversations", params={"account_id": "acc-1"}
    )
    assert unknown.status_code == 200
    assert unknown.json()["data"] == []

    denied = await client.post(
        "/api/v1/web/operations/conversations/chat-1/takeover",
        params={"account_id": "acc-1"},
        json={"state": "human"},
    )
    assert denied.status_code == 403

    missing = await client.post(
        "/api/v1/web/operations/conversations/chat-1/takeover",
        params={"account_id": "acc-1"},
        json={"state": "human"},
        headers={"X-CSRF-Token": csrf},
    )
    assert missing.status_code == 404


async def test_operator_reply_requires_confirmation_and_never_stores_plaintext(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == "acc-1"))
        ).scalar_one()
        session.add(
            Conversation(
                account_id=account.id,
                external_conversation_id="chat-1",
                bot_state="enabled",
                takeover_state="human",
            )
        )
        await session.commit()

    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    secret_text = "买家专属回复内容-不应入库"

    unconfirmed = await client.post(
        "/api/v1/web/operations/conversations/chat-1/reply",
        params={"account_id": "acc-1"},
        json={"text": secret_text},
        headers={"X-CSRF-Token": csrf},
    )
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["detail"]["code"] == "REPLY_CONFIRMATION_REQUIRED"

    queued = await client.post(
        "/api/v1/web/operations/conversations/chat-1/reply",
        params={"account_id": "acc-1"},
        json={"text": secret_text, "confirm": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert queued.status_code == 200
    assert queued.json()["data"]["status"] == "pending"

    async with get_async_session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "message.operator_reply_queued")
            )
        ).scalar_one()
    assert audit.actor == "web"
    assert audit.params is not None
    assert secret_text not in str(audit.params)
    assert audit.params["content_length"] == len(secret_text)


async def test_delivery_dry_run_requires_admin_and_changes_no_state(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    denied = await client.get(
        "/api/v1/web/operations/delivery/jobs", params={"account_id": "acc-1"}
    )
    assert denied.status_code == 401

    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    jobs = await client.get("/api/v1/web/operations/delivery/jobs", params={"account_id": "acc-1"})
    assert jobs.status_code == 200
    assert jobs.json()["data"] == []

    plan = await client.post(
        "/api/v1/web/operations/delivery/plan",
        json={"account_id": "acc-1", "order_id": "order-does-not-exist"},
    )
    assert plan.status_code == 200
    assert plan.json()["data"]["allowed"] is False

    after = await client.get("/api/v1/web/operations/delivery/jobs", params={"account_id": "acc-1"})
    assert after.json()["data"] == []


async def test_real_delivery_requires_csrf_and_confirmation(client: httpx.AsyncClient) -> None:
    await domain_accounts.create_account("acc-1")
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    payload = {"account_id": "acc-1", "order_id": "order-1"}

    no_csrf = await client.post(
        "/api/v1/web/operations/delivery/run", json={**payload, "confirm": True}
    )
    assert no_csrf.status_code == 403

    unconfirmed = await client.post(
        "/api/v1/web/operations/delivery/run",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert unconfirmed.status_code == 400
    assert unconfirmed.json()["detail"]["code"] == "DELIVERY_CONFIRMATION_REQUIRED"

    queued = await client.post(
        "/api/v1/web/operations/delivery/run",
        json={**payload, "confirm": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert queued.status_code == 200
    assert queued.json()["data"]["status"] == "pending"

    async with get_async_session() as session:
        audit = (
            await session.execute(select(AuditLog).where(AuditLog.action == "delivery.queued"))
        ).scalar_one()
    assert audit.actor == "web"
    assert audit.target == "order-1"
    assert audit.result == "queued"


async def test_delivery_api_configuration_and_secrets_never_expose_plaintext(
    client: httpx.AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    plaintext = "supplier-secret-value"

    denied = await client.post(
        "/api/v1/web/operations/delivery/api-secrets",
        json={"value": plaintext, "label": "供应商 A"},
    )
    assert denied.status_code == 403

    secret = await client.post(
        "/api/v1/web/operations/delivery/api-secrets",
        json={"value": plaintext, "label": "供应商 A"},
        headers={"X-CSRF-Token": csrf},
    )
    assert secret.status_code == 200, secret.text
    assert plaintext not in secret.text
    ref = secret.json()["data"]["ref"]

    config = await client.post(
        "/api/v1/web/operations/delivery/api-configs",
        json={
            "name": "供应商 A",
            "url": "https://cards.example.com/dispatch",
            "headers": {"Authorization": "Bearer {{secret:" + ref + "}}"},
            "body": '{"order": "{{order_id}}"}',
            "response_path": "data.card",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert config.status_code == 200, config.text
    listed = await client.get("/api/v1/web/operations/delivery/api-configs")
    assert listed.status_code == 200
    row = listed.json()["data"][0]
    assert row["name"] == "供应商 A"
    assert row["header_names"] == ["Authorization"]
    assert row["secret_refs"] == [ref]
    assert row["secrets_configured"] is True
    assert "headers" not in row
    assert "body" not in row
    assert plaintext not in listed.text

    pool = await client.post(
        "/api/v1/web/operations/delivery/pools",
        json={
            "name": "供应商 A 发卡池",
            "type": "api",
            "account_scope": "acc-1",
            "api_config_ref": "供应商 A",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert pool.status_code == 200, pool.text
    pool_id = pool.json()["data"]["id"]
    updated_pool = await client.patch(
        f"/api/v1/web/operations/delivery/pools/{pool_id}",
        json={"name": "供应商 A 发卡池-新", "sku_scope": "颜色=蓝色"},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated_pool.status_code == 200, updated_pool.text
    assert updated_pool.json()["data"]["name"] == "供应商 A 发卡池-新"
    listed_after_update = await client.get(
        "/api/v1/web/operations/delivery/pools", params={"account_id": "acc-1"}
    )
    assert listed_after_update.json()["data"][0]["sku_scope"] == "颜色=蓝色"
    policy = await client.post(
        "/api/v1/web/operations/delivery/policies",
        json={
            "name": "账号 API 自动发货",
            "inventory_pool_id": pool_id,
            "account_id": "acc-1",
            "item_id": "item-1",
            "sku_id": "颜色=蓝色",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert policy.status_code == 200, policy.text
    policy_id = policy.json()["data"]["id"]
    updated_policy = await client.patch(
        f"/api/v1/web/operations/delivery/policies/{policy_id}",
        json={"name": "账号 API 自动发货-新", "description": "按规格发货"},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated_policy.status_code == 200, updated_policy.text
    assert updated_policy.json()["data"]["sku_id"] == "颜色=蓝色"
    pools = await client.get(
        "/api/v1/web/operations/delivery/pools", params={"account_id": "acc-1"}
    )
    policies = await client.get(
        "/api/v1/web/operations/delivery/policies", params={"account_id": "acc-1"}
    )
    assert pools.json()["data"][0]["api_config_ref"] == "供应商 A"
    assert policies.json()["data"][0]["inventory_pool_id"] == pool_id
    assert policies.json()["data"][0]["sku_id"] == "颜色=蓝色"

    replacement = "replacement-secret-value"
    rotated = await client.put(
        f"/api/v1/web/operations/delivery/api-secrets/{ref}",
        json={"value": replacement},
        headers={"X-CSRF-Token": csrf},
    )
    assert rotated.status_code == 200
    assert replacement not in rotated.text

    async with get_async_session() as session:
        audits = list(
            (
                await session.execute(select(AuditLog).where(AuditLog.action.like("delivery.%")))
            ).scalars()
        )
    assert {audit.action for audit in audits} == {
        "delivery.api_secret.created",
        "delivery.api_secret.rotated",
        "delivery.api_config.created",
        "delivery.pool.created",
        "delivery.pool.updated",
        "delivery.policy.created",
        "delivery.policy.updated",
    }
    assert plaintext not in str([audit.params for audit in audits])
    assert replacement not in str([audit.params for audit in audits])


async def test_delivery_pool_delete_requires_csrf_and_keeps_reference_guard(
    client: httpx.AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    created = await client.post(
        "/api/v1/web/operations/delivery/pools",
        json={
            "name": "可删池",
            "type": "data",
            "enabled": False,
            "sku_scope": "颜色=红色|版本=专业",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    pool_id = created.json()["data"]["id"]
    listed = await client.get("/api/v1/web/operations/delivery/pools")
    assert listed.status_code == 200
    assert listed.json()["data"][0]["sku_scope"] == "颜色=红色|版本=专业"

    denied = await client.delete(f"/api/v1/web/operations/delivery/pools/{pool_id}")
    assert denied.status_code == 403

    deleted = await client.delete(
        f"/api/v1/web/operations/delivery/pools/{pool_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"] == {"id": pool_id, "deleted": True}


async def test_platform_automation_web_keeps_calibration_gate_closed(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    path = "/api/v1/web/operations/platform-automation"

    blocked = await client.post(
        path,
        params={"account_id": "acc-1"},
        json={"action": "polish", "enabled": True, "schedule_time": "03:00"},
        headers=headers,
    )
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "PLATFORM_AUTOMATION_REJECTED"

    unconfirmed = await client.post(
        f"{path}/calibration",
        params={"account_id": "acc-1"},
        json={"action": "polish"},
        headers=headers,
    )
    assert unconfirmed.status_code == 400
    approved = await client.post(
        f"{path}/calibration",
        params={"account_id": "acc-1"},
        json={"action": "polish", "confirm": True},
        headers=headers,
    )
    assert approved.status_code == 200
    assert approved.json()["data"] == {
        "action": "polish",
        "enabled": False,
        "calibrated": True,
    }

    enabled = await client.post(
        path,
        params={"account_id": "acc-1"},
        json={"action": "polish", "enabled": True, "schedule_time": "03:00"},
        headers=headers,
    )
    assert enabled.status_code == 200
    status = await client.get(path, params={"account_id": "acc-1"})
    assert status.json()["data"][0]["enabled"] is True
    assert status.json()["data"][0]["calibrated"] is True

    async with get_async_session() as session:
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action.like("platform_automation.%"))
                )
            ).scalars()
        )
    assert {row.action for row in audits} == {
        "platform_automation.calibration_approved",
        "platform_automation.configured",
    }


async def test_platform_automation_runs_are_visible_without_idempotency_keys(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        "request_flower",
        target_id="order-1",
        idempotency_key="request_flower:order-1",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="BuyerFlowerError")
    await client.post(
        "/api/v1/web/auth/login",
        json={"password": "correct horse battery staple"},
    )

    response = await client.get(
        "/api/v1/web/operations/platform-automation/runs",
        params={"account_id": "acc-1"},
    )

    assert response.status_code == 200
    assert response.json()["data"][0] == {
        "id": run_id,
        "action": "request_flower",
        "target_id": "order-1",
        "status": "uncertain",
        "result": "BuyerFlowerError",
        "started_at": response.json()["data"][0]["started_at"],
        "finished_at": response.json()["data"][0]["finished_at"],
    }
    assert "idempotency_key" not in response.text


async def test_api_config_trial_call_returns_only_a_masked_preview(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await ApiConfigService().create(
        name="发卡", url=CARD_API_URL, body='{"orderId": "{{order_id}}"}', response_path="card"
    )
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    config_id = (await ApiConfigService().list_configs())[0].local_id

    module = __import__(
        "xianyu_agent.application.delivery.api_config_service",
        fromlist=["ApiDeliveryProvider"],
    )
    monkeypatch.setattr(
        module,
        "ApiDeliveryProvider",
        type(
            "_Stub",
            (),
            {"for_config": staticmethod(lambda _config: ApiDeliveryProvider(resolve_dns=False))},
        ),
    )
    with respx.mock:
        respx.post(url__startswith=CARD_API_URL).mock(
            return_value=httpx.Response(200, json={"card": "SECRET-CARD-9876"})
        )
        response = await client.post(
            f"/api/v1/web/operations/delivery/api-configs/{config_id}/test",
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ok"] is True
    assert data["length"] == len("SECRET-CARD-9876")
    assert data["preview"] != "SECRET-CARD-9876"
    assert "SECRET-CARD-9876" not in response.text
    async with get_async_session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "delivery.api_config.tested")
            )
        ).scalar_one()
    assert "SECRET-CARD-9876" not in str(audit.params)


async def test_delivery_api_configuration_has_explicit_web_update(
    client: httpx.AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    path = "/api/v1/web/operations/delivery/api-configs"
    created = await client.post(
        path,
        json={"name": "可更新发卡源", "url": "https://cards.example.com/update"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    config_id = created.json()["data"]["id"]
    updated = await client.put(
        f"{path}/{config_id}",
        json={"description": "已更新", "timeout_seconds": 15},
        headers={"X-CSRF-Token": csrf},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == "可更新发卡源"


async def test_delivery_api_configuration_can_be_deleted_from_the_console(
    client: httpx.AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    path = "/api/v1/web/operations/delivery/api-configs"
    created = await client.post(
        path,
        json={
            "name": "待删除发卡源",
            "url": "https://cards.example.com/delete-test",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    config_id = created.json()["data"]["id"]

    denied = await client.delete(f"{path}/{config_id}")
    assert denied.status_code == 403
    removed = await client.delete(f"{path}/{config_id}", headers={"X-CSRF-Token": csrf})

    assert removed.status_code == 200, removed.text
    assert removed.json()["data"] == {"id": config_id, "deleted": True}
    assert (await client.get(path)).json()["data"] == []


async def test_card_units_can_be_imported_and_listed_masked(
    client: httpx.AsyncClient,
) -> None:
    pool = await InventoryService().create_pool(name="卡券池", type_="data")
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    path = f"/api/v1/web/operations/delivery/pools/{pool.local_id}/units"

    imported = await client.post(
        path,
        json={"contents": "CARD-AAAA-1111\nCARD-BBBB-2222\nCARD-AAAA-1111\n"},
        headers={"X-CSRF-Token": csrf},
    )

    assert imported.status_code == 200
    assert imported.json()["data"] == {
        "added": 2,
        "skipped_duplicates": 1,
        # 末尾换行由 splitlines 吃掉,不计为空白行。
        "skipped_blank": 0,
    }
    listed = await client.get(path)
    assert listed.status_code == 200
    assert listed.json()["counts"]["available"] == 2
    assert "CARD-AAAA-1111" not in listed.text
    # 掩码契约:只保留首尾各 2 个字符,中间用 *** 代替。
    assert {row["preview"] for row in listed.json()["data"]} == {"CA***11", "CA***22"}


async def test_delivery_templates_can_be_managed_from_the_console(
    client: httpx.AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    path = "/api/v1/web/operations/delivery/templates"

    created = await client.post(
        path,
        json={"name": "标准卡密", "content": "你好 {buyer_name},卡密 {card}"},
        headers={"X-CSRF-Token": csrf},
    )

    assert created.status_code == 200
    assert created.json()["data"]["variables"] == ["buyer_name", "card"]
    template_id = created.json()["data"]["id"]
    listed = await client.get(path)
    assert [row["name"] for row in listed.json()["data"]] == ["标准卡密"]

    updated = await client.put(
        f"{path}/{template_id}",
        json={"content": "已更新 {card}", "description": "更新后的说明"},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["variables"] == ["card"]

    disabled = await client.post(
        f"{path}/{template_id}/enabled",
        json={"enabled": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"] == {"id": template_id, "enabled": False}
    # 没有启用策略引用时可以删除;有引用时服务层会拒绝(见模板库单测)。
    removed = await client.delete(f"{path}/{template_id}", headers={"X-CSRF-Token": csrf})
    assert removed.status_code == 200
    assert (await client.get(path)).json()["data"] == []

    invalid = await client.post(
        path,
        json={"name": "坏模板", "content": "你好 {nickname}"},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "TEMPLATE_REJECTED"


async def test_uncertain_automation_run_can_be_resolved_from_the_console(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        "request_flower",
        target_id="order-9",
        idempotency_key="request_flower:order-9",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="transport")
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    path = f"/api/v1/web/operations/platform-automation/runs/{run_id}/resolve"

    missing_reason = await client.post(
        path, json={"status": "completed", "note": ""}, headers={"X-CSRF-Token": csrf}
    )
    assert missing_reason.status_code == 422

    resolved = await client.post(
        path,
        json={"status": "completed", "note": "平台订单页已确认发货"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resolved.status_code == 200
    assert resolved.json()["data"]["status"] == "completed"
    again = await client.post(
        path,
        json={"status": "failed", "note": "重复收尾"},
        headers={"X-CSRF-Token": csrf},
    )
    assert again.status_code == 400
    assert again.json()["detail"]["code"] == "AUTOMATION_RUN_RESOLVE_REJECTED"
    async with get_async_session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "platform_automation.run_resolved")
            )
        ).scalar_one()
    assert audit.target == f"run:{run_id}"


async def test_attention_queue_is_exposed_to_the_console(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    service = PlatformAutomationService()
    run_id = await service.reserve_run(
        "acc-1",
        "request_flower",
        target_id="order-11",
        idempotency_key="request_flower:order-11",
    )
    assert run_id is not None
    await service.finish_run(run_id, status="uncertain", result="transport")
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})

    response = await client.get("/api/v1/web/operations/attention")

    assert response.status_code == 200
    rows = response.json()["data"]
    assert [row["kind"] for row in rows] == ["automation_run"]
    assert rows[0]["target"] == f"#{run_id} request_flower order-11"
    assert "automation resolve" in rows[0]["next_command"]
    assert "result" not in rows[0]


async def test_item_automation_override_from_the_console(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id="I-1",
                title="测试商品",
                price="9.9",
                status="1",
                detail_url=None,
                main_image_url=None,
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
        ],
    )
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    path = "/api/v1/web/operations/items/automation"

    missing_reason = await client.post(
        path,
        json={"account_id": "acc-1", "item_id": "I-1", "mode": "keep"},
        headers={"X-CSRF-Token": csrf},
    )
    assert missing_reason.status_code == 400

    created = await client.post(
        path,
        json={
            "account_id": "acc-1",
            "item_id": "I-1",
            "mode": "exclude",
            "note": "活动商品",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200
    listed = await client.get(path, params={"account_id": "acc-1"})
    assert listed.json()["data"] == [
        {
            "item_id": "I-1",
            "title": "测试商品",
            "mode": "exclude",
            "note": "活动商品",
            "by": "web-admin",
            "at": listed.json()["data"][0]["at"],
        }
    ]
    cleared = await client.post(
        path,
        json={"account_id": "acc-1", "item_id": "I-1", "mode": "clear"},
        headers={"X-CSRF-Token": csrf},
    )
    assert cleared.status_code == 200
    assert (await client.get(path, params={"account_id": "acc-1"})).json()["data"] == []
    async with get_async_session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "item.automation_override_set")
            )
        ).scalars()
    assert "活动商品" not in str([row.params for row in audit])


async def test_dashboard_analytics_endpoint(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/dashboard/analytics")).status_code == 401
    await domain_accounts.create_account("acc-1")
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})

    response = await client.get(
        "/api/v1/dashboard/analytics", params={"account_id": "acc-1", "days": "1,7"}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert [window["days"] for window in data["windows"]] == [1, 7]
    assert data["automation"]["jobs_waiting_human"] == 0
    assert data["data_quality"] == {"orders_missing_paid_at": 0, "orders_missing_delivered_at": 0}
    assert data["on_sale_items"] == 0
    assert len(data["revenue_daily"]) == 7
    assert all(row["amount"] == 0 for row in data["revenue_daily"])
    assert set(data["revenue_daily"][0]) == {"date", "amount", "paid_orders"}

    bad_window = await client.get("/api/v1/dashboard/analytics", params={"days": "week"})
    assert bad_window.status_code == 400
    assert bad_window.json()["detail"]["code"] == "INVALID_WINDOW"

    missing_account = await client.get("/api/v1/dashboard/analytics", params={"account_id": "nope"})
    assert missing_account.status_code == 400


async def test_dashboard_custom_statistics_range(client: httpx.AsyncClient) -> None:
    url = "/api/v1/dashboard/analytics"
    dates = {"start_date": "2026-01-01", "end_date": "2026-01-03"}
    assert (await client.get(url, params=dates)).status_code == 401
    await domain_accounts.create_account("acc-1")
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    response = await client.get(url, params={**dates, "account_id": "acc-1"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["date_range"] == {"start": "2026-01-01", "end": "2026-01-03"}
    assert [row["days"] for row in data["windows"]] == [3]
    assert [row["date"] for row in data["revenue_daily"]] == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]
    for params in [
        {"start_date": "2026-01-01"},
        {"end_date": "2026-01-03"},
        {"start_date": "2026-01-04", "end_date": "2026-01-03"},
        {"start_date": "2025-01-01", "end_date": "2026-01-03"},
        {"start_date": "2999-01-01", "end_date": "2999-01-03"},
    ]:
        invalid = await client.get(url, params=params)
        assert invalid.status_code == 400
        assert invalid.json()["detail"] == {"code": "ANALYTICS_REJECTED"}
    assert (await client.get(url, params={"start_date": "2026-02-30"})).status_code == 422


async def test_quick_replies_and_buyer_notes_from_the_console(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    created = await client.post(
        "/api/v1/web/operations/chat/quick-replies",
        json={"title": "问候", "content": "您好,在的"},
        headers=headers,
    )
    assert created.status_code == 200
    reply_id = created.json()["data"]["id"]
    duplicate = await client.post(
        "/api/v1/web/operations/chat/quick-replies",
        json={"title": "问候", "content": "您好,在的"},
        headers=headers,
    )
    assert duplicate.status_code == 400
    listed = await client.get(
        "/api/v1/web/operations/chat/quick-replies", params={"account_id": "acc-1"}
    )
    assert [row["title"] for row in listed.json()["data"]] == ["问候"]

    missing = await client.get(
        "/api/v1/web/operations/chat/buyer-notes",
        params={"account_id": "acc-1", "buyer_id": "B-1"},
    )
    assert missing.status_code == 200
    assert missing.json()["data"] is None
    saved = await client.put(
        "/api/v1/web/operations/chat/buyer-notes",
        json={"account_id": "acc-1", "buyer_id": "B-1", "note": "定金已付"},
        headers=headers,
    )
    assert saved.status_code == 200
    shown = await client.get(
        "/api/v1/web/operations/chat/buyer-notes",
        params={"account_id": "acc-1", "buyer_id": "B-1"},
    )
    assert shown.json()["data"]["note"] == "定金已付"

    removed = await client.delete(
        f"/api/v1/web/operations/chat/quick-replies/{reply_id}", headers=headers
    )
    assert removed.status_code == 200
    assert removed.json()["data"] == {"id": reply_id, "deleted": True}
    async with get_async_session() as session:
        audits = list(
            (
                await session.execute(select(AuditLog).where(AuditLog.action.like("chat.%")))
            ).scalars()
        )
    assert {row.action for row in audits} == {
        "chat.quick_reply_created",
        "chat.quick_reply_deleted",
        "chat.buyer_note_saved",
    }
    # 备注正文不进审计参数。
    assert "定金已付" not in str([row.params for row in audits])


async def test_account_reply_pause_from_the_console(client: httpx.AsyncClient) -> None:
    await domain_accounts.create_account("acc-1")
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    path = "/api/v1/web/operations/accounts/acc-1/reply-pause"

    paused = await client.post(
        path, json={"minutes": 30, "reason": "人工处理风控"}, headers=headers
    )
    assert paused.status_code == 200
    assert paused.json()["data"]["paused_until"] is not None

    resumed = await client.post(path, json={}, headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["data"]["paused_until"] is None

    missing = await client.post(
        "/api/v1/web/operations/accounts/nope/reply-pause", json={}, headers=headers
    )
    assert missing.status_code == 404
    async with get_async_session() as session:
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "account.reply_pause_changed")
                )
            ).scalars()
        )
    # 原因正文不进审计参数,只记有没有原因。
    assert "人工处理风控" not in str([row.params for row in audits])
    assert [row.params["paused"] for row in audits] == [True, False]


async def test_log_summary_and_risk_events_endpoints(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    log_file = tmp_path / "xianyu-agent.log"
    log_file.write_text(
        "2026-09-21 10:00:00 INFO daemon started\n"
        "2026-09-21 10:00:05 ERROR 任务失败 cookie=super-secret-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))

    summary = await client.get("/api/v1/web/operations/logs/summary")

    assert summary.status_code == 200
    data = summary.json()["data"]
    if data["exists"]:
        assert data["counts"]["INFO"] == 1
        assert data["counts"]["ERROR"] == 1
        # 日志摘要只能给出脱敏后的告警行。
        assert "super-secret-value" not in summary.text
    else:
        # 宿主日志目录不同时也要给出明确结构,而不是报错。
        assert data["counts"] == {}

    async with get_async_session() as session, session.begin():
        session.add(
            AuditLog(
                actor="system",
                action="ws_user_validate_circuit_opened",
                target="acc-1",
                params={"risk_code": "FAIL_SYS_USER_VALIDATE"},
                result="blocked",
            )
        )
    risk = await client.get("/api/v1/web/operations/risk-events")

    assert risk.status_code == 200
    assert [row["action"] for row in risk.json()["data"]] == ["ws_user_validate_circuit_opened"]
    filtered = await client.get(
        "/api/v1/web/operations/risk-events", params={"account_id": "other"}
    )
    assert filtered.json()["data"] == []

    async with get_async_session() as session, session.begin():
        session.add(
            AuditLog(
                actor="system",
                action="ws_validation_recovery_probe",
                target="probe-account",
                params={},
                result="failed",
            )
        )
    probe = await client.get(
        "/api/v1/web/operations/risk-events", params={"account_id": "probe-account"}
    )
    assert probe.status_code == 200
    assert [row["action"] for row in probe.json()["data"]] == ["ws_validation_recovery_probe"]


async def test_rules_endpoint_exposes_the_reply_image_flag(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_rules.create_rule(
        "发货说明",
        "keyword",
        "发货",
        "这是你的卡密:",
        account_id="acc-1",
        reply_image_url="https://img.example/card.png",
    )
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})

    response = await client.get("/api/v1/web/operations/rules", params={"account_id": "acc-1"})

    assert response.status_code == 200
    assert response.json()["data"][0]["reply_image_url"] == "https://img.example/card.png"


async def test_messages_endpoint_exposes_inbound_image_url(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    async with get_async_session() as session, session.begin():
        account = (
            await session.execute(select(Account).where(Account.account_id == "acc-1").limit(1))
        ).scalar_one()
        conversation = ConversationRow(account_id=account.id, external_conversation_id="chat-1")
        session.add(conversation)
        await session.flush()
        session.add(
            MessageRow(
                account_id=account.id,
                conversation_id=conversation.id,
                message_id="pnm-image-1",
                chat_id="chat-1",
                direction="inbound",
                content_type="image",
                content="[图片]",
                image_url="https://img.example/buyer.png",
                received_at=datetime.now(UTC),
            )
        )
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})

    response = await client.get("/api/v1/web/operations/messages", params={"account_id": "acc-1"})

    assert response.status_code == 200
    assert response.json()["data"][0]["image_url"] == "https://img.example/buyer.png"


async def test_agent_facing_endpoints_cover_run_now_and_lifecycle_preview(
    client: httpx.AsyncClient,
) -> None:
    """这三个端点让 Agent 不必回到 CLI 也能监控与介入(它们会自动进 MCP)。"""
    await domain_accounts.create_account("acc-1")
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id="I-1",
                title="测试商品",
                price="9.9",
                status="1",
                detail_url=None,
                main_image_url=None,
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
        ],
    )
    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    plan = await client.get(
        "/api/v1/web/operations/item-lifecycle/plan",
        params={"account_id": "acc-1", "delist_after_days": 30},
    )
    assert plan.status_code == 200
    assert plan.json()["data"]["scanned"] == 1
    assert plan.json()["data"]["truncated"] is False

    # observe 模式下 run-now 必须被拒绝,而不是悄悄排一个永远不会执行的任务。
    blocked = await client.post(
        "/api/v1/web/operations/platform-automation/run-now",
        json={"account_id": "acc-1", "action": "polish"},
        headers=headers,
    )
    assert blocked.status_code == 400
    assert blocked.json()["detail"]["code"] == "RUN_NOW_REJECTED"

    # 资料刷新是只读平台调用:没有 Cookie 时如实报错,而不是假装成功。
    profile = await client.post(
        "/api/v1/web/operations/accounts/acc-1/refresh-profile", headers=headers
    )
    assert profile.status_code == 400
    assert profile.json()["detail"]["code"] == "PROFILE_REFRESH_FAILED"


async def test_delivery_job_resolve_requires_csrf_and_settles_stock(
    client: httpx.AsyncClient,
) -> None:
    """结果不明的发货任务要能在界面上收尾,但只能收尾、不能借它触发任何平台动作。"""
    await domain_accounts.create_account("acc-1")
    pool = await InventoryService().create_pool(name="卡密池", type_="data")
    await InventoryImportService().import_units(pool.local_id, ["CARD-WEB-1"])
    reservation = await InventoryReservationService().reserve(pool.local_id, "job", 1)
    service = DeliveryJobService()
    job = await service.create_or_get(account_id="acc-1", order_id="O-WEB")
    assert job.local_id is not None
    await service.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    await service.set_state(job.local_id, DeliveryJobState.RESERVED.value)
    await service.attach_reservation(job.local_id, reservation.local_id)
    await InventoryReservationService().mark_uncertain(reservation.local_id)
    await service.register_failure(job.local_id, FailureKind.UNKNOWN, error="发送后连接中断")

    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    payload = {"delivered": True, "note": "平台订单页已确认收到卡密"}
    denied = await client.post(
        f"/api/v1/web/operations/delivery/jobs/{job.local_id}/resolve", json=payload
    )
    assert denied.status_code == 403

    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    missing_note = await client.post(
        f"/api/v1/web/operations/delivery/jobs/{job.local_id}/resolve",
        json={"delivered": True, "note": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert missing_note.status_code == 422

    resolved = await client.post(
        f"/api/v1/web/operations/delivery/jobs/{job.local_id}/resolve",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert resolved.status_code == 200
    assert resolved.json()["data"]["state"] == DeliveryJobState.COMPLETED.value
    reservation_after = await InventoryReservationService().get(reservation.local_id)
    assert reservation_after.state == "consumed"

    # 已经收尾过的任务再收一次要被拒绝,免得把历史事实覆盖掉。
    twice = await client.post(
        f"/api/v1/web/operations/delivery/jobs/{job.local_id}/resolve",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert twice.status_code == 400


async def test_reply_records_are_masked_and_reset_requires_csrf(
    client: httpx.AsyncClient,
) -> None:
    """回复投递记录:未登录拒绝、预览脱敏、清空要 CSRF,且清空只删去重记录。"""
    await domain_accounts.create_account("acc-1")
    rule = await domain_rules.create_rule(
        "默认回复", "default", "", "你好呀,这是一条比较长的默认回复文案", account_id="acc-1"
    )
    await domain_rules.record_reply_log(
        account_id="acc-1",
        message_id=None,
        rule_id=rule.id,
        sent_text="你好呀,这是一条比较长的默认回复文案",
        success=True,
    )

    denied = await client.get(
        "/api/v1/web/operations/chat/reply-records", params={"account_id": "acc-1"}
    )
    assert denied.status_code == 401

    login = await client.post(
        "/api/v1/web/auth/login", json={"password": "correct horse battery staple"}
    )
    csrf = login.json()["data"]["csrf_token"]
    listed = await client.get(
        "/api/v1/web/operations/chat/reply-records", params={"account_id": "acc-1"}
    )
    assert listed.status_code == 200
    row = listed.json()["data"][0]
    assert row["rule_name"] == "默认回复"
    assert row["text_length"] > 8
    assert row["text_preview"].endswith("…")
    assert "默认回复文案" not in row["text_preview"]

    denied_reset = await client.post(
        "/api/v1/web/operations/chat/reply-records/reset", json={"account_id": "acc-1"}
    )
    assert denied_reset.status_code == 403

    reset = await client.post(
        "/api/v1/web/operations/chat/reply-records/reset",
        json={"account_id": "acc-1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert reset.status_code == 200
    assert reset.json()["data"]["removed"] == 1
    # 规则本身不受影响,只清去重记录。
    assert [item.name for item in await domain_rules.list_rules(account_id="acc-1")] == ["默认回复"]


async def test_message_workspace_filters_chat_before_limiting_and_isolates_accounts(
    client: httpx.AsyncClient,
) -> None:
    for account_id in ("acc-1", "acc-2"):
        await domain_accounts.create_account(account_id)
    async with get_async_session() as session, session.begin():
        accounts = {
            row.account_id: row.id for row in (await session.execute(select(Account))).scalars()
        }
        for account_id, chat_id, message_id in (
            ("acc-1", "selected", "selected-message"),
            ("acc-1", "other", "other-message"),
            ("acc-2", "selected", "other-account-message"),
        ):
            conversation = ConversationRow(
                account_id=accounts[account_id], external_conversation_id=chat_id
            )
            session.add(conversation)
            await session.flush()
            session.add(
                MessageRow(
                    account_id=accounts[account_id],
                    conversation_id=conversation.id,
                    message_id=message_id,
                    chat_id=chat_id,
                    direction="inbound",
                    content_type="text",
                    content=message_id,
                    received_at=datetime.now(UTC),
                )
            )
    params = {"account_id": "acc-1", "chat_id": "selected", "limit": 1}
    assert (await client.get("/api/v1/web/operations/messages", params=params)).status_code == 401
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    response = await client.get("/api/v1/web/operations/messages", params=params)
    assert response.status_code == 200
    assert [row["content"] for row in response.json()["data"]] == ["selected-message"]
    empty = await client.get(
        "/api/v1/web/operations/messages",
        params={"account_id": "acc-1", "chat_id": "missing"},
    )
    assert empty.json()["data"] == []


async def test_order_details_requires_admin_and_disables_contact_caching(
    client, monkeypatch
) -> None:

    calls = []

    async def details(self, account_id, order_id):
        calls.append((account_id, order_id))
        return {"order": {"data": {"receiver_phone": "138****5678"}, "error": None}}

    monkeypatch.setattr(XianyuOrderReadClient, "fetch_details", details)
    path = "/api/v1/web/operations/orders/O-1/details?account_id=acc-1"
    assert (await client.get(path)).status_code == 401
    assert calls == []
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    result = await client.get(path)
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store, private"
    assert result.json()["data"]["order"]["data"]["receiver_phone"] == "138****5678"
    assert calls == [("acc-1", "O-1")]


async def test_readonly_profile_requires_admin_and_never_writes_nickname_or_audit(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    await domain_accounts.create_account("acc-1")
    calls = []

    async def fetch_profile(self, account_id):
        calls.append(account_id)
        return UserProfile(nickname="平台卖家", avatar_url="https://img.example/avatar.png")

    monkeypatch.setattr(operations_module.XianyuUserProfileClient, "fetch_profile", fetch_profile)
    path = "/api/v1/web/operations/accounts/acc-1/profile"
    assert (await client.get(path)).status_code == 401
    assert calls == []
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.json()["data"]["avatar_url"] == "https://img.example/avatar.png"
    assert calls == ["acc-1"]
    assert (await domain_accounts.get_account("acc-1")).nickname is None
    async with get_async_session() as session:
        assert (
            not (await session.execute(select(AuditLog).where(AuditLog.target == "acc-1")))
            .scalars()
            .all()
        )
    assert (await client.get("/api/v1/web/operations/accounts/missing/profile")).status_code == 404
    assert calls == ["acc-1"]


async def test_readonly_profile_error_is_sanitized(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    await domain_accounts.create_account("acc-1")

    async def fail(self, account_id):
        raise UserProfileError("private-platform-response")

    monkeypatch.setattr(operations_module.XianyuUserProfileClient, "fetch_profile", fail)
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    response = await client.get("/api/v1/web/operations/accounts/acc-1/profile")
    assert response.status_code == 400
    assert response.json()["detail"] == {"code": "PROFILE_READ_FAILED"}
    assert "private-platform-response" not in response.text


async def test_item_images_include_gallery_and_skus_and_respect_account_scope(
    client: httpx.AsyncClient,
) -> None:

    for account_id in ("acc-1", "acc-2"):
        await domain_accounts.create_account(account_id)
        await domain_items.apply_on_sale_snapshot(
            account_id,
            [
                RemoteItem(
                    item_id="I-1",
                    title="商品",
                    price="9",
                    status="1",
                    detail_url=None,
                    main_image_url=f"https://img.example/{account_id}.png",
                    category_id=None,
                    auction_type=None,
                    raw_payload={"private": "not-for-web"},
                )
            ],
        )
    await domain_items.set_item_details("acc-1", "I-1", images=["https://img.example/detail.png"])
    row = await domain_items.get_item_by_external_id("acc-1", "I-1")
    await domain_items.replace_platform_variants(
        row.id,
        [
            ItemVariant(
                specs=(SpecValue("颜色", "蓝色"),),
                external_sku_id="sku-1",
                stock=1,
                image_url="https://img.example/blue.png",
            )
        ],
    )
    path = "/api/v1/web/operations/items/acc-1/I-1/images"
    assert (await client.get(path)).status_code == 401
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    response = await client.get(path)
    assert response.status_code == 200
    assert response.json()["data"] == {
        "main_image_url": "https://img.example/acc-1.png",
        "images": ["https://img.example/detail.png"],
        "platform_images": [],
        "platform_error": None,
        "variant_images": [{"label": "颜色: 蓝色", "url": "https://img.example/blue.png"}],
    }
    other = (await client.get("/api/v1/web/operations/items/acc-2/I-1/images")).json()["data"]
    assert other["images"] == []
    assert other["variant_images"] == []
    assert other["main_image_url"] == "https://img.example/acc-2.png"
    assert (
        await client.get("/api/v1/web/operations/items/acc-1/missing/images")
    ).status_code == 404
    projection = (
        await client.get("/api/v1/web/operations/items", params={"account_id": "acc-1"})
    ).json()["data"][0]
    assert projection["main_image_url"] == "https://img.example/acc-1.png"
    assert projection["images"] == ["https://img.example/detail.png"]
    assert "raw_payload" not in projection
    assert "not-for-web" not in response.text


async def test_platform_image_read_is_optional_private_and_preserves_local_gallery(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id="I-1",
                title="商品",
                price="9",
                status="1",
                detail_url=None,
                main_image_url="https://img.example/main.png",
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
        ],
    )
    await domain_items.set_item_details("acc-1", "I-1", images=["https://img.example/local.png"])
    calls = []

    async def fetch(self, account_id, item_id):
        calls.append((account_id, item_id))
        return ItemDetailFacts(item_id=item_id, images=("https://img.example/remote.png",))

    monkeypatch.setattr(operations_module.ItemDetailService, "fetch_facts", fetch)
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    path = "/api/v1/web/operations/items/acc-1/I-1/images"
    await client.get(path)
    assert calls == []
    response = await client.get(path, params={"platform": True})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.json()["data"]["platform_images"] == ["https://img.example/remote.png"]
    assert calls == [("acc-1", "I-1")]
    assert (await domain_items.get_item_by_external_id("acc-1", "I-1")).images == [
        "https://img.example/local.png"
    ]

    async def fail(self, account_id, item_id):
        raise ItemDetailError("private-platform-error")

    monkeypatch.setattr(operations_module.ItemDetailService, "fetch_facts", fail)
    failed = await client.get(path, params={"platform": True})
    assert failed.status_code == 200
    assert failed.json()["data"]["images"] == ["https://img.example/local.png"]
    assert failed.json()["data"]["platform_error"]
    assert failed.json()["data"]["platform_images"] == []
    assert "private-platform-error" not in failed.text


async def test_image_pool_projection_exposes_only_configured_picture_in_visible_scope(
    client: httpx.AsyncClient,
) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_accounts.create_account("acc-2")
    await InventoryService().create_pool(
        name="配图池",
        type_="image",
        image_url="https://img.example/delivery.png",
        account_scope="acc-1",
    )
    await InventoryService().create_pool(
        name="其他账号配图",
        type_="image",
        image_url="https://img.example/other.png",
        account_scope="acc-2",
    )
    await InventoryService().create_pool(name="卡密池", type_="data", account_scope="acc-1")
    path = "/api/v1/web/operations/delivery/pools"
    assert (await client.get(path, params={"account_id": "acc-1"})).status_code == 401
    await client.post("/api/v1/web/auth/login", json={"password": "correct horse battery staple"})
    data = (await client.get(path, params={"account_id": "acc-1"})).json()["data"]
    assert len(data) == 2
    image = next(row for row in data if row["type"] == "image")
    assert image["image_url"] == "https://img.example/delivery.png"
    assert next(row for row in data if row["type"] == "data")["image_url"] is None
    assert "content" not in image
    assert "https://img.example/other.png" not in str(data)
