"""Phase 3 CLI demo: rule-based auto-reply through the full worker pipeline.

Run: uv run python scripts/demo_reply.py
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
            "uv",
            "run",
            "xianyu-agent",
            "auth",
            "login",
            "--account",
            "demo",
            "--cookie",
            "unb=demo123; _m_h5_tk=fake_seed_xyz; cookie2=abc",
            "--remark",
            "phase3 demo",
        ]
    )
    # 规则:关键词 + 默认兜底
    run(
        [
            "uv",
            "run",
            "xianyu-agent",
            "rule",
            "add",
            "--name",
            "在吗规则",
            "--type",
            "keyword",
            "--pattern",
            "还在吗",
            "--reply",
            "在的,亲,9.9 包邮,需要吗?",
            "--account",
            "demo",
            "--priority",
            "10",
        ]
    )
    run(
        [
            "uv",
            "run",
            "xianyu-agent",
            "rule",
            "add",
            "--name",
            "默认规则",
            "--type",
            "default",
            "--pattern",
            "",
            "--reply",
            "您好,请问需要什么帮助?",
            "--priority",
            "999",
        ]
    )
    run(["uv", "run", "xianyu-agent", "rule", "list"])
    run(["uv", "run", "xianyu-agent", "rule", "test", "--content", "你好,还在吗?", "--account", "demo"])
    # 注入 5 帧 fixture(含 1 条命中规则的买家消息)
    run(
        [
            "uv",
            "run",
            "xianyu-agent",
            "protocol",
            "inject",
            "--account",
            "demo",
            "--fixture",
            "tests/fixtures/sample_frames.jsonl",
            "--count",
            "1",
        ]
    )
    run(["uv", "run", "xianyu-agent", "message", "list", "--account", "demo", "--since", "10m"])

    # 验证 reply_logs
    db = DATA / "xianyu.db"
    conn = sqlite3.connect(db)
    print("\n== reply_logs ==")
    for r in conn.execute(
        "SELECT id, rule_id, sent_text, success, source FROM reply_logs ORDER BY id"
    ):
        print(r)
    print("== reply_rules (hits) ==")
    for r in conn.execute("SELECT id, name, type, hit_count, enabled FROM reply_rules ORDER BY id"):
        print(r)
    conn.close()
    print("\nDEMO COMPLETED")


if __name__ == "__main__":
    main()