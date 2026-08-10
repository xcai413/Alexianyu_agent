"""Phase 2 CLI demo: 3 accounts online via pool, against a local mock WS server.

Run: uv run python scripts/demo_pool.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data_demo"
os.environ["XIANYU_DATA_DIR"] = str(DATA)
os.environ["XIANYU_FERNET_KEY"] = Fernet.generate_key().decode()


def run(cmd, *, check=True, env=None):
    print(f"\n>>> {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env or os.environ, check=False)
    print(r.stdout.rstrip())
    if r.returncode != 0:
        print("STDERR:", r.stderr.rstrip())
        if check:
            sys.exit(r.returncode)
    return r


def mock_server_loop(url_setter):
    """Run a websockets server in a background thread."""

    async def handler(ws):
        try:
            await ws.send(
                json.dumps(
                    {
                        "code": 0,
                        "body": {
                            "bizType": "text",
                            "1": "demo 买家消息",
                            "2": "buyer-d",
                            "4": "text",
                            "6": {"mid": f"DEMO-{id(ws)}"},
                            "10": "chat-demo",
                        },
                    }
                )
            )
            async for _ in ws:
                pass
        except Exception:
            pass
        finally:
            await ws.close()

    async def main():
        import websockets

        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            host, port = srv.sockets[0].getsockname()[:2]
            url_setter(f"ws://{host}:{port}")
            print(f"[demo] mock server at ws://{host}:{port}")
            await asyncio.Future()  # run forever

    asyncio.run(main())


def main() -> None:
    if DATA.exists():
        import shutil

        shutil.rmtree(DATA)
    ws_url: dict[str, str] = {}
    t = threading.Thread(target=mock_server_loop, args=(lambda u: ws_url.update(url=u),), daemon=True)
    t.start()
    # wait for server url
    for _ in range(100):
        if "url" in ws_url:
            break
        import time

        time.sleep(0.1)
    if "url" not in ws_url:
        print("mock server failed to start")
        sys.exit(1)
    os.environ["XIANYU_WS_URL"] = ws_url["url"]

    run(["uv", "run", "alembic", "upgrade", "head"])
    for i in range(3):
        run(
            [
                "uv",
                "run",
                "xianyu-agent",
                "auth",
                "login",
                "--account",
                f"acc-{i}",
                "--cookie",
                f"unb=acc-{i}; _m_h5_tk=seed_{i}_xyz; cookie2=abc",
                "--remark",
                f"demo {i}",
            ]
        )
    print("\n>>> pool start-all (8s, refresh 1s)")
    r = run(
        [
            "uv",
            "run",
            "xianyu-agent",
            "pool",
            "start-all",
            "--seconds",
            "8",
            "--refresh",
            "1",
        ],
        check=False,
    )
    print("\n>>> pool status (cross-process)")
    run(["uv", "run", "xianyu-agent", "pool", "status"])
    print("\n>>> message list (all accounts)")
    for i in range(3):
        run(["uv", "run", "xianyu-agent", "message", "list", "--account", f"acc-{i}", "--since", "10m"])
    print("\nDEMO COMPLETED")


if __name__ == "__main__":
    main()