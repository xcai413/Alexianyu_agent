"""测试套件共享清理。"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from xianyu_agent.application.pacing import reset_pacer
from xianyu_agent.db import database as db_mod


def _snapshot_repo_databases() -> dict[str, tuple[int, int]]:
    data_dir = Path("data")
    if not data_dir.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in sorted(data_dir.glob("*.db")):
        stat = path.stat()
        snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


@pytest.fixture(scope="session", autouse=True)
def guard_repo_databases():
    """测试不该写仓库 data/ 下的真实数据库:结束时若被动过就明确失败。

    2026-09-19 的教训:忘了给用例设置临时数据库路径时,用例会安静地写进真实库。
    这里不改变任何用例的环境,只在最后核对文件大小与修改时间。
    """
    before = _snapshot_repo_databases()
    yield
    after = _snapshot_repo_databases()
    changed = sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )
    if changed:
        msg = (
            "测试改动了仓库 data/ 下的真实数据库,请给用例设置 XIANYU_DB_PATH "
            f"指向临时目录: {', '.join(changed)}"
        )
        raise RuntimeError(msg)


@pytest.fixture(autouse=True)
def isolate_platform_pacer():
    """节流器是进程内单例,测试之间不共享令牌,否则后面的用例会真的等待。"""
    reset_pacer()
    yield
    reset_pacer()


@pytest_asyncio.fixture(autouse=True)
async def dispose_database_engine_after_test():
    """Dispose the global async engine after each test."""
    yield
    await db_mod.async_engine.dispose()
