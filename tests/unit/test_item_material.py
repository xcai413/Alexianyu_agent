"""Tests for the local material library and its use in the publish path."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from xianyu_agent.application.item import ItemMaterialService, ItemPublishService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.item.material import (
    Material,
    default_name,
    find_material,
    hash_content,
    is_supported_image,
    set_material_enabled,
)
from xianyu_agent.domain.item.publish_job import PublishDraft
from xianyu_agent.protocol.mtop.endpoints.item_publish import (
    ItemPublishError,
    PublishResult,
    UploadedImage,
)


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "materials.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


class _FakeWriter:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []
        self.published: list[object] = []

    async def upload_image(
        self, account_id: str, *, filename: str, content: bytes, content_type: str = "image/jpeg"
    ) -> UploadedImage:
        self.uploads.append((account_id, filename, content))
        return UploadedImage(url=f"https://img.example/{filename}", width=8, height=8)

    async def publish_item(self, account_id: str, request: object) -> PublishResult:
        self.published.append(request)
        return PublishResult(item_id="I-9", item_url="https://www.goofish.com/item?id=I-9")


def _material(**overrides: object) -> Material:
    values: dict[str, object] = {
        "account_id": "acc-1",
        "name": "主图",
        "path": "/tmp/a.jpg",
        "content_hash": "sha256:abc",
        "size_bytes": 10,
    }
    values.update(overrides)
    return Material(**values)  # type: ignore[arg-type]


def test_material_validation_and_helpers() -> None:
    assert hash_content(b"abc") == hash_content(b"abc")
    assert hash_content(b"abc") != hash_content(b"abd")
    assert hash_content(b"abc").startswith("sha256:")
    assert is_supported_image("A.PNG") is True
    assert is_supported_image("clip.mp4") is False
    assert default_name("bundles/images/a.jpg") == "a.jpg"
    assert default_name(r"images\b.jpg") == "b.jpg"

    with pytest.raises(ValueError, match="素材名不能为空"):
        _material(name="  ")
    with pytest.raises(ValueError, match="路径不能为空"):
        _material(path=" ")
    with pytest.raises(ValueError, match="内容哈希"):
        _material(content_hash="")
    with pytest.raises(ValueError, match="大小不能为负数"):
        _material(size_bytes=-1)
    with pytest.raises(ValueError, match="未知素材类型"):
        _material(kind="video")
    with pytest.raises(ValueError, match="不能超过"):
        _material(name="x" * 200)


@pytest.mark.asyncio
async def test_import_deduplicates_by_content_and_updates_the_name(
    clean_db, tmp_path: Path
) -> None:
    service = ItemMaterialService()
    image = tmp_path / "a.png"
    image.write_bytes(b"png-bytes")

    first = await service.import_file("acc-1", image, tags=("主图",))
    second = await service.import_file("acc-1", image, name="新名字")
    listed = await service.list("acc-1")

    assert first.local_id == second.local_id
    assert second.name == "新名字"
    assert second.size_bytes == len(b"png-bytes")
    assert len(listed) == 1
    assert listed[0].tags == ("主图",)


@pytest.mark.asyncio
async def test_import_rejects_missing_empty_and_unsupported_files(clean_db, tmp_path: Path) -> None:
    service = ItemMaterialService()
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    with pytest.raises(ValueError, match="不存在"):
        await service.import_file("acc-1", tmp_path / "missing.jpg")
    with pytest.raises(ValueError, match="文件为空"):
        await service.import_file("acc-1", empty)
    with pytest.raises(ValueError, match="暂不支持该素材类型"):
        await service.import_file("acc-1", video)


@pytest.mark.asyncio
async def test_disable_hides_a_material_from_lookup_but_keeps_the_row(
    clean_db, tmp_path: Path
) -> None:
    service = ItemMaterialService()
    image = tmp_path / "a.jpg"
    image.write_bytes(b"jpeg-bytes")
    material = await service.import_file("acc-1", image)
    assert material.local_id is not None

    disabled = await service.set_enabled(material.local_id, False)
    enabled_list = await service.list("acc-1")
    everything = await service.list("acc-1", enabled_only=False)

    assert disabled is not None
    assert disabled.enabled is False
    assert enabled_list == ()
    assert len(everything) == 1
    assert await find_material("acc-1", "a.jpg") is None
    assert await set_material_enabled(999, True) is None


@pytest.mark.asyncio
async def test_resolve_path_prefers_real_files_then_the_library(clean_db, tmp_path: Path) -> None:
    service = ItemMaterialService()
    image = tmp_path / "a.jpg"
    image.write_bytes(b"jpeg-bytes")
    await service.import_file("acc-1", image, name="主图")

    assert await service.resolve_path("acc-1", str(image)) == str(image)
    assert await service.resolve_path("acc-1", "主图") == str(image)
    assert await service.resolve_path("acc-1", "a.jpg") == str(image)
    assert await service.resolve_path("acc-1", "unknown.jpg") is None

    image.unlink()
    assert await service.resolve_path("acc-1", "主图") is None


@pytest.mark.asyncio
async def test_publish_runner_uploads_images_resolved_from_the_library(
    clean_db, tmp_path: Path
) -> None:
    service = ItemMaterialService()
    image = tmp_path / "a.jpg"
    image.write_bytes(b"jpeg-bytes")
    await service.import_file("acc-1", image, name="主图")
    writer = _FakeWriter()
    draft = PublishDraft(
        account_id="acc-1",
        title="帆布包",
        price=Decimal("45"),
        quantity=1,
        images=("主图",),
        source_row=2,
    )

    item_id = await ItemPublishService(writer, materials=service).as_publisher()(draft)

    assert item_id == "I-9"
    assert writer.uploads == [("acc-1", "a.jpg", b"jpeg-bytes")]


@pytest.mark.asyncio
async def test_publish_runner_fails_when_an_image_cannot_be_resolved(tmp_path: Path) -> None:
    writer = _FakeWriter()
    publish = ItemPublishService(writer).as_publisher()
    draft = PublishDraft(
        account_id="acc-1",
        title="帆布包",
        price=Decimal("45"),
        quantity=1,
        images=("主图",),
        source_row=2,
    )

    with pytest.raises(ItemPublishError, match="也不在素材库里"):
        await publish(draft)
    assert writer.published == []
