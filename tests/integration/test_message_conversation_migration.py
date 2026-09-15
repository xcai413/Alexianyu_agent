"""Upgrade and downgrade coverage for the Phase 3 conversation backfill."""

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


def test_message_conversation_migration_backfills_existing_history(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "migration.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db_path))
    reset_settings_cache()
    config = _alembic_config(db_path)
    command.upgrade(config, "b4c2e8f19a60")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        account_id = connection.execute(
            text(
                "INSERT INTO accounts (account_id, enabled, desired_state, status) "
                "VALUES ('seller-a', 1, 'stopped', 'offline')"
            )
        ).lastrowid
        connection.execute(
            text(
                "INSERT INTO messages "
                "(account_id, chat_id, message_id, sender_id, direction, content_type, content, processed) "
                "VALUES (:account_id, 'chat-a', 'message-a', 'buyer-a', 'inbound', 'text', 'hello', 0)"
            ),
            {"account_id": account_id},
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        message = (
            connection.execute(text("SELECT external_message_id, conversation_id FROM messages"))
            .mappings()
            .one()
        )
        conversation = (
            connection.execute(
                text(
                    "SELECT id, external_conversation_id, buyer_id, unread_count, bot_state, takeover_state "
                    "FROM conversations"
                )
            )
            .mappings()
            .one()
        )
    assert message["external_message_id"] == "message-a"
    assert message["conversation_id"] == conversation["id"]
    assert conversation == {
        "id": conversation["id"],
        "external_conversation_id": "chat-a",
        "buyer_id": "buyer-a",
        "unread_count": 0,
        "bot_state": "enabled",
        "takeover_state": "bot",
    }

    command.downgrade(config, "b4c2e8f19a60")
    assert "conversations" not in inspect(engine).get_table_names()
    engine.dispose()
    reset_settings_cache()
