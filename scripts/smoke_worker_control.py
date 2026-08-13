"""P0.2 真实账号级跨进程控制冒烟测试。

仅在用户明确授权真实测试后手动运行。脚本不安装常驻服务,结束时会请求 daemon 有序停止。
"""

from __future__ import annotations

import argparse
import asyncio
import locale
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select

from xianyu_agent.db import Account, WorkerCommand, get_async_session
from xianyu_agent.domain import daemon as daemon_domain
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.utils.process_utils import pid_alive

ROOT = Path(__file__).resolve().parents[1]


def _safe_print(value: str, *, stream=None) -> None:
    target = stream or sys.stdout
    encoding = target.encoding or "utf-8"
    safe = value.encode(encoding, errors="backslashreplace").decode(encoding)
    target.write(f"{safe}\n")
    target.flush()


def _run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    output_encoding = locale.getpreferredencoding(False)
    result = subprocess.run(
        [sys.executable, "-m", "xianyu_agent.cli.main", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding=output_encoding,
        errors="replace",
        check=False,
    )
    _safe_print(result.stdout.strip())
    if result.stderr.strip():
        _safe_print(result.stderr.strip(), stream=sys.stderr)
    if check and result.returncode != 0:
        msg = f"CLI failed ({result.returncode}): {' '.join(args)}"
        raise RuntimeError(msg)
    return result


async def _daemon_online() -> bool:
    row = await daemon_domain.latest_instance()
    return bool(row and row.status == "running" and pid_alive(row.pid))


async def _account_state(account_id: str) -> tuple[str, str]:
    rows = await AccountPool().status()
    for row in rows:
        if row["account_id"] == account_id:
            return str(row["desired_state"]), str(row["db_status"])
    msg = f"account not found: {account_id}"
    raise RuntimeError(msg)


async def _wait_state(
    account_id: str,
    *,
    desired: str,
    actual: str,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    last: tuple[str, str] | None = None
    while time.monotonic() < deadline:
        last = await _account_state(account_id)
        if last == (desired, actual):
            return
        await asyncio.sleep(0.5)
    msg = f"state timeout account={account_id} expected={(desired, actual)} last={last}"
    raise TimeoutError(msg)


async def _recent_commands(account_id: str) -> list[str]:
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id))
        ).scalar_one()
        rows = list(
            (
                await session.execute(
                    select(WorkerCommand)
                    .where(WorkerCommand.account_id == account.id)
                    .order_by(WorkerCommand.id.desc())
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
    return [f"{row.action}:{row.status}:{row.result or '-'}" for row in reversed(rows)]


async def _wait_daemon(timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await _daemon_online():
            return
        await asyncio.sleep(0.5)
    msg = "daemon did not become online"
    raise TimeoutError(msg)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stdout = (logs / "worker-control-smoke.stdout.log").open("w", encoding="utf-8")
    stderr = (logs / "worker-control-smoke.stderr.log").open("w", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    daemon = subprocess.Popen(
        [sys.executable, "-m", "xianyu_agent.cli.main", "daemon", "run"],
        cwd=ROOT,
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
    )
    try:
        asyncio.run(_wait_daemon(args.timeout))
        desired, actual = asyncio.run(_account_state(args.account))
        if desired == "running":
            asyncio.run(
                _wait_state(
                    args.account,
                    desired="running",
                    actual="connected",
                    timeout_s=args.timeout,
                )
            )
            _safe_print("STEP_INITIAL_ONLINE=PASS")
            _run_cli("pool", "stop", "--account", args.account, "--wait", "15")
            asyncio.run(
                _wait_state(
                    args.account,
                    desired="stopped",
                    actual="disconnected",
                    timeout_s=args.timeout,
                )
            )
            _safe_print("STEP_STOP=PASS")
        elif desired == "stopped" and actual == "disconnected":
            _safe_print("STEP_INITIAL_STOPPED=PASS")
        else:
            msg = f"unexpected initial state: desired={desired} actual={actual}"
            raise RuntimeError(msg)

        _run_cli("pool", "start", "--account", args.account, "--wait", "15")
        asyncio.run(
            _wait_state(
                args.account,
                desired="running",
                actual="connected",
                timeout_s=args.timeout,
            )
        )
        _safe_print("STEP_START=PASS")

        _run_cli("pool", "restart", "--account", args.account, "--wait", "15")
        asyncio.run(
            _wait_state(
                args.account,
                desired="running",
                actual="connected",
                timeout_s=args.timeout,
            )
        )
        _safe_print("STEP_RESTART=PASS")
        _safe_print("COMMANDS=" + ",".join(asyncio.run(_recent_commands(args.account))))
        return 0
    finally:
        _run_cli("daemon", "stop", check=False)
        try:
            daemon.wait(timeout=20)
        except subprocess.TimeoutExpired:
            daemon.terminate()
            daemon.wait(timeout=10)
        stdout.close()
        stderr.close()
        _safe_print(f"DAEMON_EXIT_CODE={daemon.returncode}")


if __name__ == "__main__":
    raise SystemExit(main())
