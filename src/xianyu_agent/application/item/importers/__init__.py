"""批量铺货导入器:把表格文件解析成发布草稿。"""

from .base import ImportResult, RejectedRow, rows_to_drafts
from .csv import parse_csv
from .tsv import parse_tsv
from .xlsx import parse_xlsx
from .zip_bundle import parse_zip_bundle

__all__ = [
    "ImportResult",
    "RejectedRow",
    "parse_csv",
    "parse_tsv",
    "parse_xlsx",
    "parse_zip_bundle",
    "rows_to_drafts",
]
