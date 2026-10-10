"""迁移往返:从空库升到 head,再降回 base,再升上来。

只用 `upgrade head` 验证不了 `downgrade()` 是否真的能跑——写错一个 drop 顺序或漏掉索引,只有
回滚时才炸。这里把整条链路跑一遍,保证"能升上去"和"能退回来"同时成立。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from xianyu_agent.config import get_settings, reset_settings_cache


def _alembic_config(db_path: Path) -> Config:
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def _tables(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _revision(db_path: Path) -> str:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            )
    finally:
        engine.dispose()


def test_full_migration_roundtrip_leaves_a_clean_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "roundtrip.db"
    # env.py 会用 get_settings().db_url 覆盖 config 里的 URL,所以必须先把环境指向临时库;
    # 否则这条测试会把迁移跑到仓库的开发库上(第一次写这测试时就是这样)。
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_path))
    reset_settings_cache()
    config = _alembic_config(db_path)
    assert get_settings().resolved_db_path == db_path

    command.upgrade(config, "head")
    at_head = _tables(db_path)
    head_revision = _revision(db_path)
    # 新版迁移引入的表必须在位,否则后面的回滚等于没验证到它们。
    assert {
        "alert_dispatches",
        "quick_replies",
        "buyer_notes",
        "delivery_templates",
        "revoked_web_sessions",
    } <= at_head
    assert len(head_revision) > 0

    command.downgrade(config, "base")
    at_base = _tables(db_path)
    # base 之后除了 alembic 自己的版本表,业务表都应该被清掉。
    assert at_base - {"alembic_version"} == set(), f"回滚后仍残留表: {sorted(at_base)}"

    command.upgrade(config, "head")
    assert _tables(db_path) == at_head
    assert _revision(db_path) == head_revision


def test_risk_revision_migration_backfills_existing_gate(tmp_path, monkeypatch):
    db_path = tmp_path / "risk-revision.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_path))
    reset_settings_cache()
    config = _alembic_config(db_path)
    command.upgrade(config, "3fad1e1165dc")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        metadata = MetaData()
        accounts = Table("accounts", metadata, autoload_with=engine)
        status = Table("worker_status", metadata, autoload_with=engine)
        with engine.begin() as connection:
            inserted = connection.execute(
                accounts.insert().values(
                    account_id="legacy-risk",
                    enabled=True,
                    desired_state="stopped",
                    status="offline",
                )
            )
            connection.execute(
                status.insert().values(
                    account_id=inserted.inserted_primary_key[0],
                    status="risk_cooling",
                    reconnect_attempts=0,
                    risk_code="FAIL_SYS_USER_VALIDATE",
                    risk_recovery_required=True,
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT risk_revision FROM worker_status"))
        assert isinstance(revision, str)
        assert len(revision) == 32
        command.downgrade(config, "3fad1e1165dc")
        assert "risk_revision" not in {
            c["name"] for c in inspect(engine).get_columns("worker_status")
        }
    finally:
        engine.dispose()
