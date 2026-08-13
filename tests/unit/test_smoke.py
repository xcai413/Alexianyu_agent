"""Phase 0 烟雾测试:确认包可导入、配置可加载、DB 可建表。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, text as sa_text
from typer.testing import CliRunner

from xianyu_agent import __version__
from xianyu_agent.cli.main import app
from xianyu_agent.config import Settings, get_settings, reset_settings_cache
from xianyu_agent.db import Account, database as db_mod
from xianyu_agent.db.models import Base


def test_version() -> None:
    """版本号应符合 semver。"""
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts), f"非预期版本号: {__version__}"


def test_settings_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings 应能从 .env / 环境变量加载,自动建 data 目录。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    s = get_settings()
    assert isinstance(s, Settings)
    assert s.data_dir.exists()
    assert s.log_level in {"DEBUG", "INFO", "WARNING", "ERROR"}
    reset_settings_cache()


def test_fernet_key_generation() -> None:
    """generate_fernet_key 应返回 44 字符 url-safe base64 字符串。"""
    key = Settings.generate_fernet_key()
    assert isinstance(key, str)
    assert len(key) > 40


def test_models_importable() -> None:
    """14 张表应全部注册到 Base.metadata。"""
    table_names = set(Base.metadata.tables.keys())
    expected = {
        "accounts",
        "cookies",
        "worker_status",
        "daemon_instances",
        "worker_commands",
        "messages",
        "items",
        "orders",
        "cards",
        "card_consumptions",
        "reply_rules",
        "reply_logs",
        "task_logs",
        "audit_logs",
    }
    missing = expected - table_names
    assert not missing, f"缺少表: {missing}"


@pytest.mark.asyncio
async def test_init_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init_db 能在临时目录建表。"""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_file))
    reset_settings_cache()

    # 强制 reload engine 以读到新 db_path

    db_mod.reset_engine()
    await db_mod.init_db()

    async with db_mod.async_engine.connect() as conn:
        result = await conn.execute(
            sa_text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        )
        rows = [r[0] for r in result.fetchall()]
    assert "accounts" in rows
    assert "messages" in rows

    # 验证可插入与查询
    async with db_mod.get_async_session() as session:
        acc = Account(account_id="test-demo", nickname="测试账号")
        session.add(acc)
        await session.commit()
        await session.refresh(acc)
        assert acc.id is not None

        stmt = select(Account).where(Account.account_id == "test-demo")
        got = (await session.execute(stmt)).scalar_one()
        assert got.nickname == "测试账号"

    await db_mod.async_engine.dispose()
    reset_settings_cache()


def test_cli_help() -> None:
    """CLI 应能正确注册 --help。"""

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "xianyu-agent" in result.output


def test_cli_version() -> None:
    """CLI 应能输出 --version。"""

    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_cli_hello() -> None:
    """Phase 0 hello 占位命令应可运行。"""

    runner = CliRunner()
    result = runner.invoke(app, ["hello"])
    assert result.exit_code == 0
    assert "xianyu-agent" in result.output
