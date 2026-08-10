"""Phase 7 demo: guardrails blocking + AI mode wiring (offline).

Run: uv run python scripts/demo_guardrails.py
"""
from __future__ import annotations

import json
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
            "--account", "demo", "--cookie", "unb=demo123; _m_h5_tk=fake_seed_xyz",
        ]
    )
    run(
        [
            "uv", "run", "xianyu-agent", "rule", "add",
            "--name", "在吗", "--type", "keyword", "--pattern", "还在吗",
            "--reply", "在的,亲",
        ]
    )
    run(
        [
            "uv", "run", "xianyu-agent", "card", "add",
            "--account", "demo", "--name", "卡", "--content", "C1",
        ]
    )
    # 1) 正常消息 -> 规则回复(guardrails 放行)
    run(
        [
            "uv", "run", "xianyu-agent", "protocol", "inject",
            "--account", "demo", "--fixture", "tests/fixtures/sample_frames.jsonl", "--count", "1",
        ]
    )
    # 2) 高额订单帧 -> guardrails 拦截发货 + 审计事件
    high = DATA / "high_order.jsonl"
    high.write_text(
        json.dumps(
            {
                "code": 0,
                "body": {
                    "bizType": "order",
                    "5": {"orderId": "O-HIGH", "buyerId": "b", "amount": 999999.0, "status": "paid"},
                    "100": {"itemId": "I-9", "title": "天价商品"},
                },
            }
        ),
        encoding="utf-8",
    )
    run(
        [
            "uv", "run", "xianyu-agent", "protocol", "inject",
            "--account", "demo", "--fixture", str(high), "--count", "1",
        ]
    )

    db = DATA / "xianyu.db"
    conn = sqlite3.connect(db)
    print("\n== audit_logs (guardrail) ==")
    for r in conn.execute(
        "SELECT actor, action, target, params FROM audit_logs WHERE action LIKE 'guardrail%' ORDER BY id"
    ):
        print(r)
    print("== orders ==")
    for r in conn.execute("SELECT order_id, amount, status, delivery_fail_reason FROM orders ORDER BY id"):
        print(r)
    print("== reply_logs ==")
    for r in conn.execute("SELECT rule_id, sent_text, success, source FROM reply_logs"):
        print(r)
    conn.close()
    print("\nDEMO COMPLETED")


if __name__ == "__main__":
    main()