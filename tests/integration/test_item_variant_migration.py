"""Upgrade and downgrade coverage for the item-side schema migrations."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from xianyu_agent.config import reset_settings_cache


def _alembic_config(db_path: Path) -> Config:
    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return config


def test_item_variant_migration_creates_and_reverts(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "variant_migration.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_path))
    reset_settings_cache()
    config = _alembic_config(db_path)
    command.upgrade(config, "7b41c9e0af52")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    assert "item_variants" not in inspect(engine).get_table_names()

    command.upgrade(config, "head")
    columns = {column["name"] for column in inspect(engine).get_columns("item_variants")}
    assert columns == {
        "id",
        "item_pk",
        "external_sku_id",
        "attributes",
        "price",
        "stock",
        "image_url",
        "enabled",
        "created_at",
        "updated_at",
    }
    with engine.begin() as connection:
        account_id = connection.execute(
            text(
                "INSERT INTO accounts (account_id, enabled, desired_state, status) "
                "VALUES ('seller-a', 1, 'stopped', 'offline')"
            )
        ).lastrowid
        item_pk = connection.execute(
            text(
                "INSERT INTO items (account_id, item_id, title, price, is_on_sale) "
                "VALUES (:account_id, 'I-1', '多规格商品', '45', 1)"
            ),
            {"account_id": account_id},
        ).lastrowid
        connection.execute(
            text(
                "INSERT INTO item_variants "
                "(item_pk, external_sku_id, attributes, price, stock, enabled) "
                "VALUES (:item_pk, 'SKU-1', '[]', '45', 2, 1)"
            ),
            {"item_pk": item_pk},
        )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT stock FROM item_variants")).scalar_one() == 2

    command.downgrade(config, "7b41c9e0af52")
    assert "item_variants" not in inspect(engine).get_table_names()
    engine.dispose()
    reset_settings_cache()


def test_batch_publish_migration_creates_and_reverts(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "batch_migration.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_path))
    reset_settings_cache()
    config = _alembic_config(db_path)
    command.upgrade(config, "2f83ba6d41c7")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    assert "batch_publish_jobs" not in inspect(engine).get_table_names()

    command.upgrade(config, "head")
    with engine.begin() as connection:
        account_id = connection.execute(
            text(
                "INSERT INTO accounts (account_id, enabled, desired_state, status) "
                "VALUES ('seller-a', 1, 'stopped', 'offline')"
            )
        ).lastrowid
        connection.execute(
            text(
                "INSERT INTO batch_publish_jobs "
                "(id, account_id, state, total, pending, succeeded, failed, skipped) "
                "VALUES ('batch-1', :account_id, 'pending', 1, 1, 0, 0, 0)"
            ),
            {"account_id": account_id},
        )
        connection.execute(
            text(
                "INSERT INTO batch_publish_entries (job_id, row_index, state, draft, attempts) "
                "VALUES ('batch-1', 0, 'pending', '{}', 0)"
            )
        )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT pending FROM batch_publish_jobs")).scalar_one() == 1

    command.downgrade(config, "2f83ba6d41c7")
    tables = inspect(engine).get_table_names()
    assert "batch_publish_entries" not in tables
    assert "batch_publish_jobs" not in tables
    engine.dispose()
    reset_settings_cache()
