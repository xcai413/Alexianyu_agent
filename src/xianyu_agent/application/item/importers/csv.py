"""逗号分隔文件的导入器。"""

from __future__ import annotations

import csv
import io

from .base import ImportResult, rows_to_drafts


def parse_csv(text: str, *, account_id: str) -> ImportResult:
    """Parse a CSV document into publish drafts."""
    return rows_to_drafts(_rows(text, delimiter=","), account_id=account_id)


def _rows(text: str, *, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter)
    return [[cell.strip() for cell in row] for row in reader]
