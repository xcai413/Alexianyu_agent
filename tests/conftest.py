"""测试套件共享清理。"""

from __future__ import annotations

import pytest_asyncio

from xianyu_agent.db import database as db_mod


@pytest_asyncio.fixture(autouse=True)
async def dispose_database_engine_after_test():
    """Dispose the global async engine after each test."""
    yield
    await db_mod.async_engine.dispose()
