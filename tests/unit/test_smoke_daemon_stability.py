"""P0-A 采样脚本的结果约束测试。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from scripts import smoke_daemon_stability as smoke


def _sample(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "daemon_instance": "d1",
        "daemon_pid": 123,
        "daemon": "online",
        "actual": "connected",
        "aligned": True,
        "reconnect_attempts": 0,
        "last_error": None,
    }
    row.update(overrides)
    return row


def test_run_samples_writes_healthy_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def online(_account: str, _timeout: float) -> dict[str, object]:
        return _sample()

    async def sample(_account: str) -> dict[str, object]:
        return _sample()

    monkeypatch.setattr(smoke, "_wait_online", online)
    monkeypatch.setattr(smoke, "_sample", sample)
    evidence = tmp_path / "evidence.jsonl"
    args = argparse.Namespace(account="a", timeout=1.0, duration=0.0, interval=1.0)

    rows, first = smoke._run_samples(args, evidence)

    assert first["daemon_instance"] == "d1"
    assert len(rows) == 1
    persisted = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()]
    assert len(persisted) == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"daemon_pid": 456}, "instance changed"),
        ({"daemon": "stale"}, "daemon unhealthy"),
        ({"actual": "reconnecting"}, "account unhealthy"),
        ({"last_error": "boom"}, "account error"),
    ],
)
def test_run_samples_fails_on_unhealthy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    message: str,
) -> None:
    async def online(_account: str, _timeout: float) -> dict[str, object]:
        return _sample()

    async def sample(_account: str) -> dict[str, object]:
        return _sample(**overrides)

    monkeypatch.setattr(smoke, "_wait_online", online)
    monkeypatch.setattr(smoke, "_sample", sample)
    evidence = tmp_path / "evidence.jsonl"
    args = argparse.Namespace(account="a", timeout=1.0, duration=1.0, interval=1.0)

    with pytest.raises(RuntimeError, match=message):
        smoke._run_samples(args, evidence)
