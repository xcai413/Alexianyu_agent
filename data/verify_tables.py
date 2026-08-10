"""verify_db.py - verify tables after alembic migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

db_path = Path("data/xianyu.db")
conn = sqlite3.connect(db_path)
try:
    rows = list(conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
    print(f"Database: {db_path}  size: {db_path.stat().st_size} bytes")
    print(f"Tables ({len(rows)}):")
    for (name,) in rows:
        if name.startswith("sqlite_") or name == "alembic_version":
            continue
        cnt = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        print(f"  - {name:24s} rows={cnt}")
finally:
    conn.close()
