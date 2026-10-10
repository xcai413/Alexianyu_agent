"""素材库:可复用的本地图片素材(按账号隔离)。

素材是本地文件在数据库里的索引条目:路径、内容哈希、大小与标签。用内容哈希去重,
同一张图重复导入不会产生第二条记录。只收录发布链路真正能用的图片类型——发布接口目前
只接受图片,收录视频只会给人"能发视频"的错觉。

删除刻意不提供:草稿里的图片引用可能还指向某个素材,删条目会让引用失效。停用才是
安全操作,停用后不会被新的发布引用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import select

from xianyu_agent.db import Account, ItemMaterial, get_async_session

MaterialKind = Literal["image"]
MATERIAL_KINDS: tuple[MaterialKind, ...] = ("image",)
IMAGE_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_NAME_LENGTH = 128


@dataclass(frozen=True)
class Material:
    """One indexed local asset."""

    account_id: str
    name: str
    path: str
    content_hash: str
    size_bytes: int = 0
    kind: MaterialKind = "image"
    tags: tuple[str, ...] = ()
    enabled: bool = True
    local_id: int | None = None
    created_at: datetime | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            msg = "account_id 不能为空"
            raise ValueError(msg)
        if not self.name.strip():
            msg = "素材名不能为空"
            raise ValueError(msg)
        if len(self.name.strip()) > MAX_NAME_LENGTH:
            msg = f"素材名不能超过 {MAX_NAME_LENGTH} 个字符"
            raise ValueError(msg)
        if not self.path.strip():
            msg = "素材路径不能为空"
            raise ValueError(msg)
        if not self.content_hash.strip():
            msg = "素材缺少内容哈希"
            raise ValueError(msg)
        if self.size_bytes < 0:
            msg = "素材大小不能为负数"
            raise ValueError(msg)
        if self.kind not in MATERIAL_KINDS:
            msg = f"未知素材类型: {self.kind}"
            raise ValueError(msg)


def hash_content(data: bytes) -> str:
    """Content hash used for de-duplication; prefixed so the algorithm is visible."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def is_supported_image(name: str) -> bool:
    """Whether a file name looks like an image the publish path can upload."""
    return name.strip().lower().endswith(IMAGE_SUFFIXES)


def default_name(path: str) -> str:
    """Material name defaults to the file name, so imports stay recognisable."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


async def upsert_material(material: Material) -> Material:
    """Insert or update one material by (account, content hash).

    Re-importing identical content keeps the existing row and refreshes its name,
    path, tags and state, so de-duplication never silently drops an update.
    """
    async with get_async_session() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == material.account_id).limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            msg = f"账号 {material.account_id} 不存在"
            raise ValueError(msg)
        row = (
            await session.execute(
                select(ItemMaterial)
                .where(ItemMaterial.account_id == account.id)
                .where(ItemMaterial.content_hash == material.content_hash)
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = ItemMaterial(
                account_id=account.id,
                content_hash=material.content_hash,
                name=material.name.strip(),
                path=material.path.strip(),
                kind=material.kind,
                size_bytes=material.size_bytes,
                tags=list(material.tags),
                enabled=material.enabled,
            )
            session.add(row)
        else:
            row.name = material.name.strip()
            row.path = material.path.strip()
            row.kind = material.kind
            row.size_bytes = material.size_bytes
            # 重复导入只增不减标签:再次导入同一张图时忘记带 --tag 不应把既有标签抹掉。
            existing_tags = row.tags if isinstance(row.tags, list) else []
            row.tags = sorted({str(tag) for tag in (*existing_tags, *material.tags)})
        await session.commit()
        await session.refresh(row)
    return _from_row(row, account_id=material.account_id)


async def list_materials(
    account_id: str, *, enabled_only: bool = True, limit: int = 200
) -> tuple[Material, ...]:
    """List one account's materials, newest first."""
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return ()
        stmt = select(ItemMaterial).where(ItemMaterial.account_id == account.id)
        if enabled_only:
            stmt = stmt.where(ItemMaterial.enabled.is_(True))
        stmt = stmt.order_by(ItemMaterial.id.desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())
    return tuple(_from_row(row, account_id=account_id) for row in rows)


async def find_material(account_id: str, reference: str) -> Material | None:
    """Find one material by name or by the file name of its path."""
    wanted = reference.strip()
    if not wanted:
        return None
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return None
        rows = list(
            (
                await session.execute(
                    select(ItemMaterial)
                    .where(ItemMaterial.account_id == account.id)
                    .where(ItemMaterial.enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
    for row in rows:
        if row.name == wanted or default_name(row.path) == wanted:
            return _from_row(row, account_id=account_id)
    return None


async def set_material_enabled(material_id: int, enabled: bool) -> Material | None:
    """Enable or disable one material; None when it does not exist."""
    async with get_async_session() as session:
        row = (
            await session.execute(
                select(ItemMaterial).where(ItemMaterial.id == material_id).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.enabled = enabled
        await session.commit()
        await session.refresh(row)
        account_id = (
            await session.execute(select(Account.account_id).where(Account.id == row.account_id))
        ).scalar_one_or_none()
    return _from_row(row, account_id=str(account_id or row.account_id))


def _from_row(row: ItemMaterial, *, account_id: str) -> Material:
    tags = row.tags if isinstance(row.tags, list) else []
    return Material(
        account_id=account_id,
        name=str(row.name),
        path=str(row.path),
        content_hash=str(row.content_hash),
        size_bytes=int(row.size_bytes or 0),
        kind=str(row.kind),  # type: ignore[arg-type]
        tags=tuple(str(tag) for tag in tags),
        enabled=bool(row.enabled),
        local_id=int(row.id),
        created_at=row.created_at,
    )
