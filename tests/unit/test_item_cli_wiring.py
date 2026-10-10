"""End-to-end CLI wiring: real services against a temporary database.

The other CLI tests replace the application services with fakes, which would hide a
mis-wired constructor. These run the actual commands against a real SQLite database
and assert the output an operator would see.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.protocol.items_client import RemoteItem

CSV_TEXT = "标题,价格,库存\n帆布包,45,3\n"


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prepare a fresh database the CLI can open from its own event loop."""
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "cli_wiring.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(_prepare())
    db_mod.reset_engine()
    yield
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


async def _prepare() -> None:
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
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
                category_id="123",
                auction_type="a",
                raw_payload=None,
            )
        ],
    )
    await db_mod.async_engine.dispose()


def test_material_commands_run_against_a_real_database(wired_db: None, tmp_path: Path) -> None:
    image = tmp_path / "bag.png"
    image.write_bytes(b"png-bytes")
    runner = CliRunner()

    imported = runner.invoke(
        app, ["item", "material", "import", "--account", "acc-1", "--file", str(image)]
    )
    listed = runner.invoke(app, ["item", "material", "list", "--account", "acc-1"])

    assert imported.exit_code == 0, imported.output
    assert "bag.png" in imported.output
    material_id = int(re.search(r"素材 #(\d+)", imported.output).group(1))  # type: ignore[union-attr]

    disabled = runner.invoke(app, ["item", "material", "disable", "--id", str(material_id)])
    hidden = runner.invoke(app, ["item", "material", "list", "--account", "acc-1"])

    assert listed.exit_code == 0, listed.output
    assert "bag.png" in listed.output
    assert disabled.exit_code == 0, disabled.output
    assert "已停用" in disabled.output
    assert "暂无素材" in hidden.output


def test_binding_commands_run_against_a_real_database(wired_db: None) -> None:
    runner = CliRunner()

    bound = runner.invoke(
        app,
        [
            "item",
            "bind",
            "--account",
            "acc-1",
            "--item-id",
            "I-1",
            "--spec",
            "颜色=红色",
            "--spec",
            "尺码=M",
            "--target",
            "card:7",
            "--quantity",
            "2",
        ],
    )
    listed = runner.invoke(app, ["item", "bindings", "--account", "acc-1", "--item-id", "I-1"])
    unbound = runner.invoke(
        app,
        [
            "item",
            "unbind",
            "--account",
            "acc-1",
            "--item-id",
            "I-1",
            "--spec",
            "颜色=红色",
            "--spec",
            "尺码=M",
        ],
    )
    empty = runner.invoke(app, ["item", "bindings", "--account", "acc-1", "--item-id", "I-1"])

    assert bound.exit_code == 0, bound.output
    assert "已绑定 颜色:红色 尺码:M -> card:7" in bound.output
    assert listed.exit_code == 0, listed.output
    assert "card:7" in listed.output
    assert "2" in listed.output
    assert unbound.exit_code == 0, unbound.output
    assert "剩余 0 条" in unbound.output
    assert "尚未配置" in empty.output


def test_batch_commands_run_against_a_real_database(wired_db: None, tmp_path: Path) -> None:
    path = tmp_path / "rows.csv"
    path.write_text(CSV_TEXT, encoding="utf-8")
    runner = CliRunner()

    imported = runner.invoke(
        app, ["item", "batch", "import", "--account", "acc-1", "--file", str(path)]
    )
    job_id = re.search(r"批次 (\w+) 已创建", imported.output)
    shown = runner.invoke(app, ["item", "batch", "show", "--job-id", job_id.group(1)])  # type: ignore[union-attr]
    recovered = runner.invoke(
        app,
        ["item", "batch", "recover", "--job-id", job_id.group(1)],  # type: ignore[union-attr]
    )
    missing = runner.invoke(app, ["item", "batch", "show", "--job-id", "nope"])

    assert imported.exit_code == 0, imported.output
    assert "可发布 1 行" in imported.output
    assert shown.exit_code == 0, shown.output
    assert "帆布包" in shown.output
    assert "pending" in shown.output
    assert recovered.exit_code == 0, recovered.output
    assert "批次已恢复" in recovered.output
    assert missing.exit_code == 1
    assert "不存在" in missing.output
