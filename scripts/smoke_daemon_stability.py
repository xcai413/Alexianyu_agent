"""P0-A 真实账号 daemon 连续运行采样与有序退出验收。"""

from __future__ import annotations

import argparse
import asyncio
import json
import locale
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from xianyu_agent.services.observability import build_runtime_snapshot

ROOT = Path(__file__).resolve().parents[1]


def _safe_print(value: str, *, stream: IO[str] | None = None) -> None:
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
    if check and result.returncode != 0:
        msg = f"CLI failed ({result.returncode}): {' '.join(args)}"
        raise RuntimeError(msg)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--duration", type=float, default=1800.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.duration <= 0 or args.interval <= 0:
        parser.error("duration and interval must be positive")
    return args


async def _sample(account_id: str) -> dict[str, object]:
    snapshot = await build_runtime_snapshot()
    account = next((row for row in snapshot.accounts if row.account_id == account_id), None)
    if account is None:
        msg = f"account not found: {account_id}"
        raise RuntimeError(msg)
    return {
        "at": datetime.now(UTC).isoformat(),
        "daemon_instance": snapshot.daemon.instance_id if snapshot.daemon else None,
        "daemon_pid": snapshot.daemon.pid if snapshot.daemon else None,
        "daemon": snapshot.daemon_health.observed,
        "daemon_heartbeat_age_s": snapshot.daemon_health.heartbeat_age_s,
        "desired": account.desired_state,
        "actual": account.actual_state,
        "aligned": account.aligned,
        "worker_heartbeat_age_s": account.heartbeat_age_s,
        "reconnect_attempts": account.reconnect_attempts,
        "last_error": account.last_error,
    }


async def _wait_online(account_id: str, timeout_s: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        last = await _sample(account_id)
        if last["daemon"] == "online" and last["actual"] == "connected":
            return last
        await asyncio.sleep(0.5)
    msg = f"daemon/account did not become online: {last}"
    raise TimeoutError(msg)


def _start_daemon(logs: Path) -> tuple[subprocess.Popen, IO[str], IO[str]]:
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stdout = (logs / "p0-a-daemon.stdout.log").open("w", encoding="utf-8")
    stderr = (logs / "p0-a-daemon.stderr.log").open("w", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    daemon = subprocess.Popen(
        [sys.executable, "-m", "xianyu_agent.cli.main", "daemon", "run"],
        cwd=ROOT,
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
    )
    return daemon, stdout, stderr


def _run_samples(args: argparse.Namespace, evidence_path: Path) -> tuple[list[dict], dict]:
    first = asyncio.run(_wait_online(args.account, args.timeout))
    expected_instance = first["daemon_instance"]
    expected_pid = first["daemon_pid"]
    samples: list[dict[str, object]] = []
    deadline = time.monotonic() + args.duration
    while True:
        sample = asyncio.run(_sample(args.account))
        samples.append(sample)
        with evidence_path.open("a", encoding="utf-8") as evidence:
            evidence.write(json.dumps(sample, ensure_ascii=False) + "\n")
        if sample["daemon_instance"] != expected_instance or sample["daemon_pid"] != expected_pid:
            msg = "daemon instance changed during P0-A"
            raise RuntimeError(msg)
        if sample["daemon"] != "online":
            msg = f"daemon unhealthy: {sample['daemon']}"
            raise RuntimeError(msg)
        if sample["actual"] != "connected" or not sample["aligned"]:
            msg = f"account unhealthy: actual={sample['actual']} aligned={sample['aligned']}"
            raise RuntimeError(msg)
        if sample["last_error"]:
            msg = f"account error: {sample['last_error']}"
            raise RuntimeError(msg)
        remaining = deadline - time.monotonic()
        if len(samples) == 1 or len(samples) % 6 == 0:
            elapsed = max(0.0, args.duration - max(0.0, remaining))
            _safe_print(
                f"PROGRESS elapsed_s={elapsed:.1f} samples={len(samples)} "
                f"daemon={sample['daemon']} account={sample['actual']} "
                f"reconnects={sample['reconnect_attempts']}"
            )
        if remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))
    return samples, first


def _print_summary(
    args: argparse.Namespace,
    samples: list[dict[str, object]],
    first: dict[str, object],
    evidence_path: Path,
) -> None:
    _safe_print("P0_A_STABILITY=PASS")
    _safe_print(f"DURATION_S={args.duration:.1f}")
    _safe_print(f"SAMPLES={len(samples)}")
    _safe_print(f"DAEMON_INSTANCE={first['daemon_instance']}")
    _safe_print(f"DAEMON_PID={first['daemon_pid']}")
    _safe_print(f"RECONNECTS={samples[-1]['reconnect_attempts']}")
    _safe_print(f"EVIDENCE={evidence_path}")


def main() -> int:
    args = _parse_args()
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    evidence_path = logs / "p0-a-daemon-stability.jsonl"
    evidence_path.write_text("", encoding="utf-8")
    daemon, stdout, stderr = _start_daemon(logs)
    passed = False
    try:
        samples, first = _run_samples(args, evidence_path)
        passed = True
        _print_summary(args, samples, first, evidence_path)
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
        _safe_print(f"ORDERLY_EXIT={'PASS' if passed and daemon.returncode == 0 else 'FAIL'}")
        _safe_print(f"DAEMON_EXIT_CODE={daemon.returncode}")


if __name__ == "__main__":
    raise SystemExit(main())
