"""全新安装冒烟:迁移到 head 之后,所有只读命令都必须能跑。

这组测试防的是"新命令只在开发库里跑过"——开发库的迁移状态往往领先或落后于 head,命令里任何一个
对新表/新列的查询在干净库上都会直接炸。这里从空库开始跑完整迁移,再逐条执行只读命令。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts

runner = CliRunner()
ACCOUNT = "acc-smoke"

# 只读命令:不接受任何真实外发、不需要先有数据,可以安全地逐条跑。
# 判据:在只跑完迁移并建了一个**未启用**账号的空库上,退出码必须是 0。
READ_ONLY_COMMANDS: tuple[list[str], ...] = (
    ["account", "list"],
    ["account", "show", "--id", ACCOUNT],
    ["attention", "list"],
    ["analytics", "report", "--account", ACCOUNT],
    ["automation", "status", "--account", ACCOUNT],
    ["automation", "runs", "--account", ACCOUNT],
    ["item", "list", "--account", ACCOUNT],
    ["item", "automation", "list", "--account", ACCOUNT],
    ["order", "list", "--account", ACCOUNT],
    ["rule", "list", "--account", ACCOUNT],
    ["rule", "logs", "--account", ACCOUNT],
    ["message", "quick-reply", "list", "--account", ACCOUNT],
    ["message", "note", "show", "--account", ACCOUNT, "--buyer-id", "buyer-1"],
    ["ai", "list"],
    ["inventory", "pool", "list"],
    ["inventory", "unit", "list", "1"],
    ["inventory", "reservation", "list"],
    ["item", "material", "list", "--account", ACCOUNT],
    ["item", "lifecycle", "plan", "--account", ACCOUNT],
    ["delivery", "job", "list"],
    ["notify", "channel", "list"],
    ["notify", "messages"],
    ["delivery", "policy", "list"],
    ["delivery", "template", "list"],
    ["delivery", "api", "list"],
    ["delivery", "guard", "list"],
    ["pool", "status"],
    ["daemon", "logs", "--stats"],
    ["doctor"],
)


def _alembic_config(db_path: Path) -> Config:
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


@pytest.fixture
def fresh_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_path))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    reset_settings_cache()
    db_mod.reset_engine()
    command.upgrade(_alembic_config(db_path), "head")
    # 建成未启用:全新安装还没有 Cookie,启用账号会让 doctor 如实报"启用但无法认证"。
    asyncio.run(domain_accounts.create_account(ACCOUNT, enabled=False))
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


@pytest.mark.parametrize("args", READ_ONLY_COMMANDS, ids=" ".join)
def test_read_only_command_works_on_a_fresh_database(
    fresh_install: Path, args: list[str]
) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 0, f"{' '.join(args)} 在全新库上失败:\n{result.output}"


def test_doctor_has_no_failure_on_a_fresh_install(fresh_install: Path) -> None:
    """全新库上只允许 WARN(没有 daemon、没配告警、非 Windows),不允许 FAIL。"""
    result = runner.invoke(app, ["doctor", "--output", "json"])

    assert result.exit_code == 0, result.output
    assert '"fail": 0' in result.output, result.output


def test_backup_export_writes_a_document_on_a_fresh_install(fresh_install: Path) -> None:
    target = fresh_install / "backup.json"

    result = runner.invoke(app, ["backup", "export", "--out", str(target)])

    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert ACCOUNT in target.read_text(encoding="utf-8")


def test_backup_import_dry_run_accepts_its_own_export(fresh_install: Path) -> None:
    """导出与导入必须互相兼容:自己导出的文件要能被自己预演导入。"""
    target = fresh_install / "backup.json"
    assert runner.invoke(app, ["backup", "export", "--out", str(target)]).exit_code == 0

    result = runner.invoke(app, ["backup", "import", "--in", str(target)])

    assert result.exit_code == 0, result.output
    assert "预演" in result.output
