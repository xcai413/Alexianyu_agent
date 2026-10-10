"""发货策略应用层与 CLI 接线(Issue #5 Phase 6, 6D)。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from xianyu_agent.application.delivery import DeliveryPolicyService
from xianyu_agent.application.inventory import InventoryService
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain.delivery import PolicyError

runner = CliRunner()


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "delivery.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(db_mod.init_db())
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


async def _seed_pool(*, type_: str = "data", enabled: bool = True, name: str = "卡密池") -> int:
    pool = await InventoryService().create_pool(name=name, type_=type_, enabled=enabled)
    return pool.local_id


async def test_create_rejects_unknown_pool_and_mode_mismatch(wired_db: Path) -> None:
    service = DeliveryPolicyService()

    with pytest.raises(PolicyError, match="库存池 #99 不存在"):
        await service.create(name="策略", inventory_pool_id=99)

    pool_id = await _seed_pool(type_="data")
    with pytest.raises(PolicyError, match="不一致"):
        await service.create(name="策略", inventory_pool_id=pool_id, delivery_mode="image")


async def test_create_list_and_update_roundtrip(wired_db: Path) -> None:
    pool_id = await _seed_pool()
    service = DeliveryPolicyService()

    created = await service.create(
        name="账号策略",
        inventory_pool_id=pool_id,
        account_id="acc-1",
        delay_seconds=30,
        template="买家{buyer_name} 卡密{card}",
    )

    assert created.local_id is not None
    listed = await service.list(only_enabled=True)
    assert [policy.name for policy in listed] == ["账号策略"]

    updated = await service.update(created.local_id, delay_seconds=0, priority=5, name="账号策略A")
    assert updated.delay_seconds == 0
    assert updated.priority == 5
    assert updated.name == "账号策略A"
    assert updated.template_variables == ("buyer_name", "card")

    scoped = await service.update(
        created.local_id,
        item_id="I-1",
        sku_id="颜色=蓝色",
        description="按规格发货",
    )
    assert scoped.item_id == "I-1"
    assert scoped.sku_id == "颜色=蓝色"
    assert scoped.description == "按规格发货"

    cleared = await service.update(created.local_id, clear_scope=True)
    assert cleared.account_id is None
    assert cleared.item_id is None
    assert cleared.sku_id is None

    with pytest.raises(ValueError, match="延迟发货秒数"):
        await service.update(created.local_id, delay_seconds=99999)
    with pytest.raises(ValueError, match="不在白名单内"):
        await service.update(created.local_id, template="{unknown_var}")


async def test_resolve_explains_the_winning_policy(wired_db: Path) -> None:
    pool_id = await _seed_pool()
    global_pool = await _seed_pool(name="全局池")
    service = DeliveryPolicyService()
    await service.create(name="全局", inventory_pool_id=global_pool)
    await service.create(name="商品", inventory_pool_id=pool_id, item_id="I-1")

    resolution = await service.resolve(account_id="acc-1", item_id="I-1")

    assert resolution.matched is True
    assert resolution.policy is not None
    assert resolution.policy.name == "商品"
    assert "入库池" in resolution.reason or "库存池" in resolution.reason


async def test_resolve_reports_disabled_pool(wired_db: Path) -> None:
    pool_id = await _seed_pool(enabled=False)
    service = DeliveryPolicyService()
    await service.create(name="停用池策略", inventory_pool_id=pool_id, item_id="I-1")

    resolution = await service.resolve(item_id="I-1")

    assert resolution.matched is False
    assert "已停用" in resolution.reason


async def test_disabled_policy_falls_back_to_global(wired_db: Path) -> None:
    pool_id = await _seed_pool()
    other_pool = await _seed_pool(name="全局池")
    service = DeliveryPolicyService()
    account_policy = await service.create(
        name="账号", inventory_pool_id=pool_id, account_id="acc-1"
    )
    await service.create(name="全局", inventory_pool_id=other_pool)

    assert (await service.resolve(account_id="acc-1")).policy.name == "账号"  # type: ignore[union-attr]

    await service.set_enabled(account_policy.local_id, enabled=False)
    assert (await service.resolve(account_id="acc-1")).policy.name == "全局"  # type: ignore[union-attr]


async def test_pool_with_policies_cannot_be_deleted(wired_db: Path) -> None:
    pool_id = await _seed_pool()
    service = DeliveryPolicyService()
    policy = await service.create(name="策略", inventory_pool_id=pool_id)

    with pytest.raises(ValueError, match="发货策略引用"):
        await InventoryService().delete_pool(pool_id)

    await service.delete(policy.local_id)
    await InventoryService().delete_pool(pool_id)


def test_cli_policy_create_list_resolve_and_render(wired_db: Path) -> None:
    assert (
        runner.invoke(
            app, ["inventory", "pool", "create", "--name", "卡密池", "--type", "data"]
        ).exit_code
        == 0
    )
    created = runner.invoke(
        app,
        [
            "delivery",
            "policy",
            "create",
            "--name",
            "账号策略",
            "--pool",
            "1",
            "--account",
            "acc-1",
            "--template",
            "买家{buyer_name} 卡密{card}",
            "--delay",
            "15",
        ],
    )
    assert created.exit_code == 0, created.output
    assert "账号" in created.output

    listed = runner.invoke(app, ["delivery", "policy", "list"])
    assert listed.exit_code == 0
    assert "账号策略" in listed.output

    shown = runner.invoke(app, ["delivery", "policy", "show", "1"])
    assert shown.exit_code == 0
    assert "buyer_name" in shown.output

    resolved = runner.invoke(app, ["delivery", "policy", "resolve", "--account", "acc-1"])
    assert resolved.exit_code == 0
    assert "命中 account 级" in resolved.output

    missing = runner.invoke(app, ["delivery", "policy", "resolve", "--account", "acc-2"])
    assert missing.exit_code == 0
    assert "未命中" in missing.output

    rendered = runner.invoke(
        app,
        ["delivery", "render", "--template", "卡密:{card}", "--var", "card=ABC-123"],
    )
    assert rendered.exit_code == 0
    assert "ABC-123" in rendered.output

    rejected = runner.invoke(
        app,
        ["delivery", "render", "--template", "{secret}", "--var", "secret=x"],
    )
    assert rejected.exit_code == 1
    assert "不在白名单内" in rejected.output


def test_cli_policy_delete_and_pool_guard(wired_db: Path) -> None:
    assert (
        runner.invoke(
            app, ["inventory", "pool", "create", "--name", "卡密池", "--type", "data"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["delivery", "policy", "create", "--name", "策略", "--pool", "1"]
        ).exit_code
        == 0
    )

    blocked = runner.invoke(app, ["inventory", "pool", "delete", "1", "--yes"])
    assert blocked.exit_code == 1
    assert "发货策略引用" in blocked.output

    deleted = runner.invoke(app, ["delivery", "policy", "delete", "1", "--yes"])
    assert deleted.exit_code == 0
    assert runner.invoke(app, ["inventory", "pool", "delete", "1", "--yes"]).exit_code == 0


def test_cli_policy_set_updates_scope_and_rejects_noop(wired_db: Path) -> None:
    assert (
        runner.invoke(
            app, ["inventory", "pool", "create", "--name", "卡密池", "--type", "data"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["delivery", "policy", "create", "--name", "策略", "--pool", "1"],
        ).exit_code
        == 0
    )

    updated = runner.invoke(
        app,
        [
            "delivery",
            "policy",
            "set",
            "1",
            "--item-id",
            "I-1",
            "--spec",
            "颜色=蓝色",
            "--desc",
            "按规格发货",
        ],
    )
    assert updated.exit_code == 0, updated.output
    shown = runner.invoke(app, ["delivery", "policy", "show", "1"])
    assert "蓝色" in shown.output
    assert "按规格发货" in shown.output

    noop = runner.invoke(app, ["delivery", "policy", "set", "1"])
    assert noop.exit_code == 1
    assert "至少提供一项" in noop.output
