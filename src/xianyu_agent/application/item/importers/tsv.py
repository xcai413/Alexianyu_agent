"""制表符分隔文件的导入器。"""

from __future__ import annotations

import csv
import io

from .base import ImportResult, rows_to_drafts


def parse_tsv(text: str, *, account_id: str) -> ImportResult:
    """Parse a TSV document into publish drafts."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter="\t")
    rows = [[cell.strip() for cell in row] for row in reader]
    return rows_to_drafts(rows, account_id=account_id)
