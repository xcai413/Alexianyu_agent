"""E2E CLI smoke test for Phase 1. Delete after verifying."""
from __future__ import annotations

import os
import subprocess
import sys

from cryptography.fernet import Fernet

os.environ["XIANYU_DATA_DIR"] = os.path.join(os.path.dirname(__file__), "..", "data_e2e")
os.environ["XIANYU_FERNET_KEY"] = Fernet.generate_key().decode()


def run(cmd, check=True):
    print(f"\n>>> {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=os.environ, check=False)
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print("STDERR:", r.stderr.rstrip())
        if check:
            sys.exit(r.returncode)
    return r


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
        "e2e test",
    ]
)
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
run(["uv", "run", "xianyu-agent", "message", "show", "1"])
run(["uv", "run", "xianyu-agent", "auth", "list"])
print("\nE2E SMOKE COMPLETED SUCCESSFULLY")