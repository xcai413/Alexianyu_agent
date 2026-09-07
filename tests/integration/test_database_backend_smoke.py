"""SQLite / MySQL / PostgreSQL 基础连通性与 metadata 冒烟测试。"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from xianyu_agent.config import get_settings
from xianyu_agent.db.database import drop_db, get_async_session, init_db


@pytest.mark.integration
@pytest.mark.asyncio
async def test_configured_database_backend_can_create_query_and_drop_schema() -> None:
    """CI 中对每个受支持数据库执行真实连接、建表、查询与清理。"""
    expected_backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not expected_backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    settings = get_settings()
    assert settings.database_backend == expected_backend

    await init_db()
    try:
        async with get_async_session() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await drop_db()
