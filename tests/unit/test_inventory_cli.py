"""CLI 接线:inventory 子命令跑真实服务与真实 SQLite 库。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod

runner = CliRunner()


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "inventory_cli.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(db_mod.init_db())
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


def _invoke(*args: str):
    result = runner.invoke(app, list(args))
    return result


def test_pool_create_list_show_and_delay(wired_db: Path) -> None:
    created = _invoke("inventory", "pool", "create", "--name", "卡密池", "--type", "data")
    assert created.exit_code == 0, created.output
    assert "data" in created.output

    listed = _invoke("inventory", "pool", "list")
    assert listed.exit_code == 0
    assert "卡密池" in listed.output

    delayed = _invoke("inventory", "pool", "delay", "1", "--seconds", "45")
    assert delayed.exit_code == 0
    assert "45s" in delayed.output

    shown = _invoke("inventory", "pool", "show", "1")
    assert shown.exit_code == 0
    assert "延迟 45s" in shown.output

    disabled = _invoke("inventory", "pool", "disable", "1")
    assert disabled.exit_code == 0
    assert "已停用" in disabled.output


def test_pool_set_renames_and_rejects_empty_update(wired_db: Path) -> None:
    created = _invoke("inventory", "pool", "create", "--name", "旧名称", "--type", "data")
    assert created.exit_code == 0, created.output

    empty = _invoke("inventory", "pool", "set", "1")
    assert empty.exit_code == 1
    assert "至少提供一项" in empty.output

    updated = _invoke("inventory", "pool", "set", "1", "--name", "新名称")
    assert updated.exit_code == 0, updated.output
    assert "新名称" in updated.output
    assert "旧名称" not in _invoke("inventory", "pool", "list").output

    scoped = _invoke("inventory", "pool", "set", "1", "--sku", "颜色=蓝色")
    assert scoped.exit_code == 0, scoped.output
    assert "颜色=蓝色" in _invoke("inventory", "pool", "show", "1").output


def test_pool_create_accepts_sku_scope(wired_db: Path) -> None:
    created = _invoke(
        "inventory",
        "pool",
        "create",
        "--name",
        "规格池",
        "--type",
        "data",
        "--sku",
        "颜色=红色|版本=专业",
    )
    assert created.exit_code == 0, created.output
    shown = _invoke("inventory", "pool", "show", "1")
    assert shown.exit_code == 0
    assert "颜色=红色|版本=专业" in shown.output


def test_pool_create_rejects_invalid_type(wired_db: Path) -> None:
    result = _invoke("inventory", "pool", "create", "--name", "卡密池", "--type", "magic")

    assert result.exit_code == 1
    assert "未知库存池类型" in result.output


def test_text_pool_holds_its_content(wired_db: Path) -> None:
    missing = _invoke("inventory", "pool", "create", "--name", "文案池", "--type", "text")
    assert missing.exit_code == 1
    assert "必须提供发货文案" in missing.output

    created = _invoke(
        "inventory",
        "pool",
        "create",
        "--name",
        "文案池",
        "--type",
        "text",
        "--content",
        "结算后发我地址",
    )
    assert created.exit_code == 0, created.output

    shown = _invoke("inventory", "pool", "show", "1")
    assert shown.exit_code == 0
    assert "结算后发我地址" in shown.output


def test_text_pool_can_be_filled_before_enabling(wired_db: Path) -> None:
    draft = _invoke(
        "inventory", "pool", "create", "--name", "文案池", "--type", "text", "--disabled"
    )
    assert draft.exit_code == 0, draft.output

    blocked = _invoke("inventory", "pool", "enable", "1")
    assert blocked.exit_code == 1
    assert "必须提供发货文案" in blocked.output

    filled = _invoke("inventory", "pool", "content", "1", "--content", "结算后发我地址")
    assert filled.exit_code == 0, filled.output

    enabled = _invoke("inventory", "pool", "enable", "1")
    assert enabled.exit_code == 0, enabled.output
    assert "已启用" in enabled.output


def test_pool_content_requires_something_to_set(wired_db: Path) -> None:
    assert (
        _invoke(
            "inventory", "pool", "create", "--name", "文案池", "--type", "text", "--disabled"
        ).exit_code
        == 0
    )

    result = _invoke("inventory", "pool", "content", "1")

    assert result.exit_code == 1
    assert "请用 --content 或 --image-url" in result.output


def test_unit_import_list_and_reserve(wired_db: Path) -> None:
    assert (
        _invoke("inventory", "pool", "create", "--name", "卡密池", "--type", "data").exit_code == 0
    )

    imported = _invoke("inventory", "unit", "import", "1", "--content", "CARD-1\nCARD-2\nCARD-1")
    assert imported.exit_code == 0, imported.output
    assert "新增 2 条" in imported.output
    assert "跳过重复 1 条" in imported.output

    listed = _invoke("inventory", "unit", "list", "1")
    assert listed.exit_code == 0
    assert "CA***-1" in listed.output

    reserved = _invoke(
        "inventory", "reservation", "reserve", "--pool", "1", "--job", "job-1", "--quantity", "1"
    )
    assert reserved.exit_code == 0, reserved.output
    assert "job-1" in reserved.output

    after = _invoke("inventory", "pool", "show", "1")
    assert "reserved=1" in after.output


def test_reserve_reports_insufficient_stock(wired_db: Path) -> None:
    assert (
        _invoke("inventory", "pool", "create", "--name", "卡密池", "--type", "data").exit_code == 0
    )
    assert _invoke("inventory", "unit", "import", "1", "--content", "ONLY-1").exit_code == 0

    result = _invoke(
        "inventory", "reservation", "reserve", "--pool", "1", "--job", "job-1", "--quantity", "3"
    )

    assert result.exit_code == 1
    assert "不足以预留" in result.output


def test_reservation_release_and_resolve(wired_db: Path) -> None:
    assert (
        _invoke("inventory", "pool", "create", "--name", "卡密池", "--type", "data").exit_code == 0
    )
    assert _invoke("inventory", "unit", "import", "1", "--content", "CARD-1\nCARD-2").exit_code == 0
    assert (
        _invoke(
            "inventory",
            "reservation",
            "reserve",
            "--pool",
            "1",
            "--job",
            "job-1",
            "--quantity",
            "2",
        ).exit_code
        == 0
    )

    released = _invoke("inventory", "reservation", "release", "1", "--yes")
    assert released.exit_code == 0
    assert "退回 2 条" in released.output

    assert (
        _invoke(
            "inventory",
            "reservation",
            "reserve",
            "--pool",
            "1",
            "--job",
            "job-2",
            "--quantity",
            "1",
        ).exit_code
        == 0
    )
    resolved = _invoke("inventory", "reservation", "resolve", "2", "--delivered", "--yes")
    assert resolved.exit_code == 0
    assert "已发送" in resolved.output

    listed = _invoke("inventory", "reservation", "list")
    assert listed.exit_code == 0
    assert "consumed" in listed.output


def test_pool_delete_requires_confirmation(wired_db: Path) -> None:
    assert (
        _invoke("inventory", "pool", "create", "--name", "卡密池", "--type", "data").exit_code == 0
    )

    aborted = _invoke("inventory", "pool", "delete", "1")
    assert aborted.exit_code == 1

    deleted = _invoke("inventory", "pool", "delete", "1", "--yes")
    assert deleted.exit_code == 0
    assert "已删除" in deleted.output


def test_reservation_consume_and_uncertain(wired_db: Path) -> None:
    assert (
        _invoke("inventory", "pool", "create", "--name", "卡密池", "--type", "data").exit_code == 0
    )
    assert _invoke("inventory", "unit", "import", "1", "--content", "CARD-1\nCARD-2").exit_code == 0
    assert (
        _invoke(
            "inventory",
            "reservation",
            "reserve",
            "--pool",
            "1",
            "--job",
            "job-1",
            "--quantity",
            "2",
        ).exit_code
        == 0
    )

    uncertain = _invoke("inventory", "reservation", "uncertain", "1")
    assert uncertain.exit_code == 0
    assert "已冻结 2 条" in uncertain.output

    shown = _invoke("inventory", "pool", "show", "1")
    assert "uncertain=2" in shown.output

    resolved = _invoke("inventory", "reservation", "resolve", "1", "--delivered", "--yes")
    assert resolved.exit_code == 0
    assert "已发送" in resolved.output

    assert (
        _invoke("inventory", "reservation", "reserve", "--pool", "1", "--job", "x").exit_code == 1
    )
