"""XLSX 导入器:只用标准库解析,不引入额外依赖。

覆盖 Excel / WPS 导出的常规工作表:共享字符串、内联字符串、数字、布尔与公式的
缓存值。公式本身不求值,只读取表格软件已经算好的结果;不认识的单元格类型按空值
处理,不会把原始 XML 当成文本塞进草稿。
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

from .base import ImportResult, rows_to_drafts

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_WORKSHEET = re.compile(r"xl/worksheets/sheet(\d+)\.xml$")
_CELL_REFERENCE = re.compile(r"([A-Z]+)\d+")
_SHARED_STRINGS = "xl/sharedStrings.xml"


def parse_xlsx(data: bytes, *, account_id: str, sheet_index: int = 0) -> ImportResult:
    """Parse one worksheet of an XLSX document into publish drafts."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            shared = _shared_strings(archive)
            rows = _worksheet_rows(archive, sheet_index=sheet_index, shared=shared)
    except zipfile.BadZipFile as exc:
        msg = "XLSX 文件不是有效的 ZIP 容器"
        raise ValueError(msg) from exc
    return rows_to_drafts(rows, account_id=account_id)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        payload = archive.read(_SHARED_STRINGS)
    except KeyError:
        return []
    root = ElementTree.fromstring(payload)
    return ["".join(node.text or "" for node in item.iter(f"{_NS}t")) for item in root]


def _worksheet_rows(
    archive: zipfile.ZipFile, *, sheet_index: int, shared: list[str]
) -> list[list[str]]:
    names = sorted(
        (name for name in archive.namelist() if _WORKSHEET.match(name)),
        key=lambda name: int(_WORKSHEET.match(name).group(1)),  # type: ignore[union-attr]
    )
    if not names:
        msg = "XLSX 内没有工作表"
        raise ValueError(msg)
    if sheet_index >= len(names):
        msg = f"XLSX 只有 {len(names)} 个工作表,无法读取第 {sheet_index + 1} 个"
        raise ValueError(msg)
    root = ElementTree.fromstring(archive.read(names[sheet_index]))
    rows: list[list[str]] = []
    for row in root.iter(f"{_NS}row"):
        cells: dict[int, str] = {}
        for cell in row.iter(f"{_NS}c"):
            index = _column_index(cell.get("r"))
            if index is None:
                continue
            cells[index] = _cell_text(cell, shared)
        if not cells:
            continue
        width = max(cells) + 1
        rows.append([cells.get(index, "") for index in range(width)])
    return rows


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_NS}t")).strip()
    value = cell.find(f"{_NS}v")
    if value is None or value.text is None:
        return ""
    text = value.text.strip()
    if kind == "s":
        try:
            return shared[int(text)].strip()
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if text in {"1", "true", "TRUE"} else "FALSE"
    return text


def _column_index(reference: str | None) -> int | None:
    match = _CELL_REFERENCE.match(reference or "")
    if match is None:
        return None
    index = 0
    for character in match.group(1):
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1
