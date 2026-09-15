"""Unit tests for configuration primitives."""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.config import (
    Settings,
    ensure_fernet_key,
    get_settings,
    reset_settings_cache,
)


def test_ensure_fernet_key_generates_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XIANYU_FERNET_KEY", raising=False)
    reset_settings_cache()

    key = ensure_fernet_key()
    assert len(key) > 40

    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert f"XIANYU_FERNET_KEY={key}" in env_file.read_text(encoding="utf-8")
    assert get_settings().fernet_key == key
    reset_settings_cache()


def test_ensure_fernet_key_keeps_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    existing = Settings.generate_fernet_key()
    (tmp_path / ".env").write_text(f"XIANYU_FERNET_KEY={existing}\n", encoding="utf-8")
    reset_settings_cache()

    key = ensure_fernet_key()
    assert key == existing
    reset_settings_cache()


def test_account_lock_path_is_collision_resistant(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    first = settings.account_lock_path("a/b")
    second = settings.account_lock_path("a?b")
    assert first != second
    assert first.parent == tmp_path / "runtime" / "accounts"
    assert "/" not in first.name
    assert "?" not in second.name


def test_default_database_url_remains_sqlite(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, database_url="", db_path=None)
    assert settings.database_backend == "sqlite"
    assert settings.db_url == f"sqlite+aiosqlite:///{(tmp_path / 'xianyu.db').as_posix()}"
    assert settings.sync_db_url == f"sqlite:///{(tmp_path / 'xianyu.db').as_posix()}"


@pytest.mark.parametrize(
    ("configured", "expected_async", "expected_sync", "backend"),
    [
        (
            "mysql://user:secret@db.example:3306/xianyu",
            "mysql+asyncmy://user:secret@db.example:3306/xianyu",
            "mysql+pymysql://user:secret@db.example:3306/xianyu",
            "mysql",
        ),
        (
            "postgresql://user:secret@db.example:5432/xianyu",
            "postgresql+asyncpg://user:secret@db.example:5432/xianyu",
            "postgresql+psycopg://user:secret@db.example:5432/xianyu",
            "postgresql",
        ),
    ],
)
def test_database_url_normalizes_supported_remote_backends(
    tmp_path: Path,
    configured: str,
    expected_async: str,
    expected_sync: str,
    backend: str,
) -> None:
    settings = Settings(data_dir=tmp_path, database_url=configured)
    assert settings.db_url == expected_async
    assert settings.sync_db_url == expected_sync
    assert settings.database_backend == backend


def test_explicit_database_url_overrides_db_path(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "ignored.db",
        database_url="sqlite:///./explicit.db",
    )
    assert settings.db_url == "sqlite+aiosqlite:///./explicit.db"
