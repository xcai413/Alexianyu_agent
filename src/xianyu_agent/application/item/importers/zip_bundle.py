"""ZIP 打包导入:一份表格加上可选的图片素材。

图片只按成员名上报,不落盘也不上传:是否上传、上传到哪里由后续的发布步骤决定。
文件名会去掉目录前缀,便于和表格里写的图片名对应。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace

from .base import ImportResult
from .csv import parse_csv
from .tsv import parse_tsv
from .xlsx import parse_xlsx

TABLE_SUFFIXES = (".xlsx", ".csv", ".tsv")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
IGNORED_NAMES = ("__MACOSX/", ".DS_Store")
MAX_TABLE_MEMBERS = 1


def parse_zip_bundle(data: bytes, *, account_id: str) -> ImportResult:
    """Parse the single tabular member and report the bundled image names."""
    if not zipfile.is_zipfile(io.BytesIO(data)):
        msg = "不是有效的 ZIP 文件"
        raise ValueError(msg)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [name for name in archive.namelist() if _is_readable(name)]
        tables = [name for name in members if name.lower().endswith(TABLE_SUFFIXES)]
        if not tables:
            msg = "ZIP 内没有找到 .xlsx / .csv / .tsv 表格文件"
            raise ValueError(msg)
        if len(tables) > MAX_TABLE_MEMBERS:
            msg = f"ZIP 内存在多个表格文件,请只放一份: {', '.join(sorted(tables))}"
            raise ValueError(msg)
        table = tables[0]
        payload = archive.read(table)
        images = tuple(
            sorted(_base_name(name) for name in members if name.lower().endswith(IMAGE_SUFFIXES))
        )
    return replace(_parse_table(table, payload, account_id=account_id), bundled_images=images)


def _parse_table(name: str, payload: bytes, *, account_id: str) -> ImportResult:
    lowered = name.lower()
    if lowered.endswith(".xlsx"):
        return parse_xlsx(payload, account_id=account_id)
    text = payload.decode("utf-8-sig", errors="replace")
    if lowered.endswith(".tsv"):
        return parse_tsv(text, account_id=account_id)
    return parse_csv(text, account_id=account_id)


def _is_readable(name: str) -> bool:
    return bool(name) and not name.endswith("/") and not name.startswith(IGNORED_NAMES)


def _base_name(name: str) -> str:
    return name.rsplit("/", 1)[-1]
