"""运营配置备份:不含凭据、不含校准、导入幂等(参考 butler backup)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from xianyu_agent.application.automation import (
    ACTION_POLISH,
    PlatformAutomationService,
)
from xianyu_agent.application.backup import SCHEMA_VERSION, BackupService
from xianyu_agent.application.delivery import (
    ApiConfigService,
    DeliveryPolicyService,
    DeliveryTemplateService,
)
from xianyu_agent.application.inventory import InventoryImportService, InventoryService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, rules as domain_rules
from xianyu_agent.domain.delivery import ApiDeliveryConfig

CARD_API_URL = "https://cards.example.com/issue"


@pytest.fixture
async def backup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "backup.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_configuration() -> None:
    await domain_accounts.create_account("acc-1", remark="主号", enabled=True)
    await domain_rules.create_rule(
        "问候", "keyword", "你好", "您好,在的", account_id="acc-1"
    )
    await ApiConfigService().create(
        name="发卡",
        url=CARD_API_URL,
        method="GET",
        headers={"X-Token": "{{secret:card_token}}"},
        body='{"orderId": "{{order_id}}"}',
        response_path="data.card",
    )
    await DeliveryTemplateService().create(name="标准卡密", content="卡密:{card}")
    pool = await InventoryService().create_pool(
        name="卡密池", type_="api", api_config_ref="发卡"
    )
    await DeliveryPolicyService().create(
        name="策略",
        inventory_pool_id=pool.local_id or 0,
        account_id="acc-1",
        template_ref="标准卡密",
    )
    # 真实校准批准记录:备份里必须看不到它。
    await PlatformAutomationService().approve_calibration(
        "acc-1", ACTION_POLISH, approved_by="operator"
    )


async def test_export_contains_configuration_but_never_credentials_or_calibration(
    backup_db: None,
) -> None:
    await _seed_configuration()

    document = await BackupService().export_document()

    assert document["schema_version"] == SCHEMA_VERSION
    assert document["includes_cards"] is False
    assert [row["account_id"] for row in document["accounts"]] == ["acc-1"]
    assert [row["name"] for row in document["delivery_templates"]] == ["标准卡密"]
    assert [row["pool_name"] for row in document["delivery_policies"]] == ["卡密池"]
    # 密钥只有引用名,没有值;Cookie / Token / 校准批准都不在文档里。
    assert document["api_configs"][0]["headers"] == {"X-Token": "{{secret:card_token}}"}
    text = str(document)
    assert "card_token" in text
    assert "calibration" not in text
    assert "cookie" not in text.lower()
    assert "token_value" not in text


async def test_export_can_include_available_cards_only_when_asked(
    backup_db: None,
) -> None:
    pool = await InventoryService().create_pool(name="数据池", type_="data")
    await InventoryImportService().import_units(pool.local_id or 0, ["CARD-1", "CARD-2"])

    without = await BackupService().export_document()
    with_cards = await BackupService().export_document(include_cards=True)

    assert without["inventory_pools"][0]["units"] == []
    assert with_cards["inventory_pools"][0]["units"] == ["CARD-1", "CARD-2"]
    assert with_cards["includes_cards"] is True


async def test_import_is_idempotent_against_existing_configuration(
    backup_db: None,
) -> None:
    await _seed_configuration()
    document = await BackupService().export_document()

    first = await BackupService().import_document(document)
    second = await BackupService().import_document(document)

    # 库里已经有同名对象:两次导入都只能跳过,不能产生第二份配置。
    assert first.total_created == 0
    assert first.total_skipped > 0
    assert second.total_created == 0
    assert second.total_skipped == first.total_skipped


async def test_import_creates_missing_configuration_on_a_fresh_database(
    backup_db: None,
) -> None:
    document = {
        "schema_version": SCHEMA_VERSION,
        "accounts": [{"account_id": "acc-7", "remark": "备份恢复", "enabled": True}],
        "reply_rules": [
            {
                "name": "问候",
                "type": "keyword",
                "pattern": "你好",
                "reply_text": "您好,在的",
                "priority": 100,
                "enabled": True,
                "reply_once": False,
                "account_id": "acc-7",
            }
        ],
        "api_configs": [
            {
                "name": "发卡",
                "url": CARD_API_URL,
                "method": "GET",
                "headers": {"X-Token": "{{secret:card_token}}"},
                "body": '{"orderId": "{{order_id}}"}',
                "response_path": "data.card",
                "timeout_seconds": 10.0,
                "enabled": True,
                "description": None,
            }
        ],
        "delivery_templates": [
            {"name": "标准卡密", "content": "卡密:{card}", "enabled": True}
        ],
        "inventory_pools": [
            {
                "name": "卡密池",
                "type": "api",
                "api_config_ref": "发卡",
                "enabled": True,
                "units": [],
            }
        ],
        "delivery_policies": [
            {
                "name": "策略",
                "pool_name": "卡密池",
                "account_id": "acc-7",
                "template_ref": "标准卡密",
                "platform_confirm": False,
                "enabled": True,
            }
        ],
    }

    report = await BackupService().import_document(document)

    assert report.created == {
        "accounts": 1,
        "api_configs": 1,
        "delivery_templates": 1,
        "inventory_pools": 1,
        "delivery_policies": 1,
        "reply_rules": 1,
    }
    assert [row.name for row in await DeliveryTemplateService().list_templates()] == ["标准卡密"]
    assert [row.name for row in await ApiConfigService().list_configs()] == ["发卡"]
    policies = await DeliveryPolicyService().list()
    assert [policy.template_ref for policy in policies] == ["标准卡密"]
    assert [row.name for row in await domain_rules.list_rules()] == ["问候"]
    # 备份里没有校准记录,导入后擦亮仍然未校准。
    automation = await PlatformAutomationService().list("acc-7")
    assert all(not row.calibrated for row in automation)


async def test_dry_run_reports_without_writing(backup_db: None) -> None:
    document = {
        "schema_version": SCHEMA_VERSION,
        "accounts": [{"account_id": "acc-9", "enabled": True}],
        "reply_rules": [],
        "api_configs": [],
        "delivery_templates": [],
        "inventory_pools": [],
        "delivery_policies": [],
    }

    report = await BackupService().import_document(document, dry_run=True)

    assert report.created.get("accounts") == 1
    assert await domain_accounts.get_account("acc-9") is None


async def test_import_rejects_unknown_schema_version(backup_db: None) -> None:
    with pytest.raises(ValueError, match="不受支持"):
        await BackupService().import_document({"schema_version": 99})


async def test_import_skips_policy_whose_pool_is_missing(backup_db: None) -> None:
    document = {
        "schema_version": SCHEMA_VERSION,
        "accounts": [],
        "reply_rules": [],
        "api_configs": [],
        "delivery_templates": [],
        "inventory_pools": [],
        "delivery_policies": [
            {"name": "孤儿策略", "pool_name": "不存在的池", "enabled": True}
        ],
    }

    report = await BackupService().import_document(document)

    assert report.created.get("delivery_policies") is None
    assert report.skipped.get("delivery_policies") == 1
    assert await DeliveryPolicyService().list() == []


async def test_api_config_export_keeps_structure_only(backup_db: None) -> None:
    config = ApiDeliveryConfig(
        name="无密钥", url=CARD_API_URL, method="POST", headers={}, body="{}"
    )
    await ApiConfigService().create(
        name=config.name,
        url=config.url,
        method=config.method,
        body=config.body,
        response_path="card",
    )

    document = await BackupService().export_document()

    assert document["api_configs"] == [
        {
            "name": "无密钥",
            "url": CARD_API_URL,
            "method": "POST",
            "headers": {},
            "body": "{}",
            "response_path": "card",
            "timeout_seconds": 10.0,
            "enabled": True,
            "description": None,
        }
    ]
