"""逐行卡密导入(Issue #5 Phase 6, 6B)。

导入是"把库存写进池"的唯一入口:直接改数据库绕过单元模型会让占用与对账失去依据。
重复行与空白行在这里被拒绝计入,统计结果回给运营,避免同一条卡密被卖两次。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.inventory import UnitImportSummary
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyInventoryRepository

MAX_IMPORT_BYTES = 8 * 1024 * 1024


def _read_lines(path: Path) -> tuple[str, ...]:
    """同步读文件:导入是一次性动作,单独放线程里没有收益,校验先做完。"""
    file_path = Path(path)
    if not file_path.is_file():
        msg = f"导入文件不存在: {file_path}"
        raise ValueError(msg)
    if file_path.stat().st_size > MAX_IMPORT_BYTES:
        msg = f"导入文件超过 {MAX_IMPORT_BYTES} 字节上限: {file_path}"
        raise ValueError(msg)
    return tuple(file_path.read_text(encoding="utf-8", errors="replace").splitlines())


class InventoryImportService:
    """Append card codes to a DATA pool."""

    async def import_units(self, pool_id: int, contents: Sequence[str]) -> UnitImportSummary:
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            summary = await repo.import_units(pool_id, contents)
            await session.commit()
        return summary

    async def import_file(self, pool_id: int, path: Path) -> UnitImportSummary:
        """Read a UTF-8 text file (one code per line) and append it."""
        return await self.import_units(pool_id, _read_lines(path))
