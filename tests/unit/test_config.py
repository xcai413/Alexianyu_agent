"""Unit tests for FERNET_KEY auto-generation (docs promise: "首次启动自动生成")."""

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
