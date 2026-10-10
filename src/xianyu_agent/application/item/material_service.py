"""素材库服务:把本地图片登记进索引,并把草稿里的素材名解析成可上传路径。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from xianyu_agent.domain.item.material import (
    Material,
    default_name,
    find_material,
    hash_content,
    is_supported_image,
    list_materials,
    set_material_enabled,
    upsert_material,
)


class ItemMaterialService:
    """Index local assets and resolve draft references to uploadable files."""

    async def import_file(
        self,
        account_id: str,
        path: str | Path,
        *,
        name: str | None = None,
        tags: Sequence[str] = (),
    ) -> Material:
        """Register one local image; identical content reuses the existing entry."""
        source = Path(path)
        if not await asyncio.to_thread(source.is_file):
            msg = f"素材文件不存在: {path}"
            raise ValueError(msg)
        if not is_supported_image(source.name):
            msg = f"暂不支持该素材类型: {source.suffix or source.name}"
            raise ValueError(msg)
        data = await asyncio.to_thread(source.read_bytes)
        if not data:
            msg = "素材文件为空"
            raise ValueError(msg)
        return await upsert_material(
            Material(
                account_id=account_id,
                name=(name or default_name(str(source))).strip(),
                path=str(source),
                content_hash=hash_content(data),
                size_bytes=len(data),
                tags=tuple(tag for tag in tags if tag.strip()),
            )
        )

    async def list(
        self, account_id: str, *, enabled_only: bool = True, limit: int = 200
    ) -> tuple[Material, ...]:
        return await list_materials(account_id, enabled_only=enabled_only, limit=limit)

    async def set_enabled(self, material_id: int, enabled: bool) -> Material | None:
        return await set_material_enabled(material_id, enabled)

    async def resolve_path(self, account_id: str, reference: str) -> str | None:
        """Resolve a draft image reference to an uploadable local file.

        A reference is used as a path when that file exists; otherwise it is looked
        up in the library by material name or file name. A library entry whose file
        has moved resolves to None rather than to a stale path.
        """
        candidate = Path(reference)
        if await asyncio.to_thread(candidate.is_file):
            return str(candidate)
        material = await find_material(account_id, reference)
        if material is None:
            return None
        if await asyncio.to_thread(Path(material.path).is_file):
            return material.path
        return None
