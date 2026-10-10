"""专用后端完成迁移后,autogenerate不能再建议删除或新增既有结构。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from xianyu_agent.config import get_settings


@pytest.mark.integration
def test_migrated_schema_matches_canonical_metadata() -> None:
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("path_separator", "os")
    command.check(config)
