"""Phase 4 CLI demo: card -> inject paid order -> auto-delivery -> order show.

Run: uv run python scripts/demo_delivery.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data_demo"
os.environ["XIANYU_DATA_DIR"] = str(DATA)
os.environ["XIANYU_FERNET_KEY"] = Fernet.generate_key().decode()


def run(cmd, *, check=True):
    print(f"\n>>> {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=os.environ, check=False)
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print("STDERR:", r.stderr.rstrip())
        if check:
            sys.exit(r.returncode)
    return r


def main() -> None:
    if DATA.exists():
        import shutil

        shutil.rmtree(DATA)
    run(["uv", "run", "alembic", "upgrade", "head"])
    run(
        [
            "uv", "run", "xianyu-agent", "auth", "login",
            "--account", "demo", "--cookie", "unb=demo123; _m_h5_tk=fake_seed_xyz; cookie2=abc",
            "--remark", "phase4 demo",
        ]
    )
    run(
        [
            "uv", "run", "xianyu-agent", "card", "add",
            "--account", "demo", "--name", "9.9元卡密", "--type", "text",
            "--content", "CARD-A001\nCARD-A002\nCARD-A003",
            "--price", "9.9",
        ]
    )
    run(["uv", "run", "xianyu-agent", "card", "list", "--account", "demo"])
    run(
        [
            "uv", "run", "xianyu-agent", "protocol", "inject",
            "--account", "demo", "--fixture", "tests/fixtures/sample_frames.jsonl", "--count", "1",
        ]
    )
    run(["uv", "run", "xianyu-agent", "order", "list", "--account", "demo"])
    run(["uv", "run", "xianyu-agent", "order", "show", "1"])

    db = DATA / "xianyu.db"
    conn = sqlite3.connect(db)
    print("\n== card_consumptions ==")
    for r in conn.execute(
        "SELECT id, card_id, order_id, content, status FROM card_consumptions ORDER BY id"
    ):
        print(r)
    print("== orders ==")
    for r in conn.execute(
        "SELECT id, order_id, status, delivery_content, delivery_fail_reason FROM orders"
    ):
        print(r)
    conn.close()
    print("\nDEMO COMPLETED")


if __name__ == "__main__":
    main()