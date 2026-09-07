"""异步数据库引擎与 session 工厂。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from xianyu_agent.config import get_settings
from xianyu_agent.db.models import Base


def _make_engine() -> AsyncEngine:
    settings = get_settings()
    url = make_url(settings.db_url)
    backend = url.get_backend_name()
    connect_args: dict[str, Any] = {}

    if backend == "sqlite":
        connect_args["check_same_thread"] = False

    engine = create_async_engine(
        settings.db_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if backend == "sqlite":

        @sa_event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_fk(dbapi_connection, _connection_record) -> None:
            """SQLite needs PRAGMA foreign_keys=ON per connection for cascades."""
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


# 全局单例引擎;测试可通过 reset_engine() 重置
async_engine: AsyncEngine = _make_engine()
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_async_session() -> AsyncIterator[AsyncSession]:
    """异步 session 上下文管理器。

    用法:
        async with get_async_session() as session:
            ...
    """
    session = async_session_factory()
    try:
        yield session
    finally:
        await session.close()


async def init_db() -> None:
    """建表(仅供测试 / Phase 0 冒烟用;正式迁移走 alembic)。"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db() -> None:
    """删表(测试用)。"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def reset_engine() -> None:
    """测试用:替换全局引擎与 session 工厂。"""
    global async_engine, async_session_factory  # noqa: PLW0603
    async_engine = _make_engine()
    async_session_factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def engine_info() -> dict[str, Any]:
    """诊断用:返回引擎与 DB 状态;远程数据库不会暴露密码。"""
    settings = get_settings()
    url = make_url(settings.db_url)
    backend = url.get_backend_name()
    info: dict[str, Any] = {
        "url": url.render_as_string(hide_password=True),
        "backend": backend,
    }
    if backend == "sqlite":
        info.update(
            {
                "db_path": str(settings.resolved_db_path),
                "exists": settings.resolved_db_path.exists(),
            }
        )
    else:
        info.update({"db_path": None, "exists": None})
    return info
