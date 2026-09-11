"""Regression coverage for canonical WorkerState health/soak compatibility."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from xianyu_agent.application.health import soak as soak_health
from xianyu_agent.application.health.observability import (
    AccountObservation,
    RuntimeSnapshot,
    _alignment,
    is_worker_healthy_online,
)
from xianyu_agent.application.health.runtime_health import DaemonHealth
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.db.models import TaskStatus
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.runtime.worker_state import WorkerState, worker_state_from_persistence

NOW = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)


def _snapshot(
    *,
    account_id: str = "acc-online",
    actual_state: str = "online",
    aligned: bool = True,
    last_error: str | None = None,
) -> RuntimeSnapshot:
    account = AccountObservation(
        account_id=account_id,
        enabled=True,
        desired_state="running",
        actual_state=actual_state,
        aligned=aligned,
        heartbeat_age_s=1.0,
        reconnect_attempts=0,
        last_heartbeat_at=NOW,
        last_error=last_error,
        risk_code=None,
        risk_cooldown_until=None,
        risk_status=None,
        issue=None if aligned else f"期望 running,实际 {actual_state}",
    )
    return RuntimeSnapshot(
        daemon=None,
        daemon_health=DaemonHealth(
            observed="online",
            active=True,
            process_alive=True,
            heartbeat_age_s=1.0,
            healthy=True,
        ),
        daemon_uptime_s=60.0,
        accounts=(account,),
    )


@pytest.mark.parametrize(
    ("actual_state", "healthy"),
    [
        ("online", True),
        ("connected", True),
        ("starting", False),
        ("checking_session", False),
        ("refreshing_credential", False),
        ("connecting", False),
        ("registering", False),
        ("syncing", False),
        ("reconnecting", False),
        ("needs_validation", False),
        ("error", False),
    ],
)
def test_observability_running_alignment_requires_online_readiness(
    actual_state: str,
    healthy: bool,
) -> None:
    assert is_worker_healthy_online(actual_state) is healthy
    aligned, issue = _alignment(
        enabled=True,
        desired_state="running",
        actual_state=actual_state,
    )
    assert aligned is healthy
    assert (issue is None) is healthy


def test_legacy_connected_stays_compatibility_healthy_without_online_promotion() -> None:
    assert worker_state_from_persistence("connected") is WorkerState.SYNCING
    assert is_worker_healthy_online("connected") is True
    assert is_worker_healthy_online(WorkerState.SYNCING.value) is False


def test_soak_sample_accepts_canonical_online_without_drift() -> None:
    snapshot = _snapshot()
    account = snapshot.accounts[0]

    sample = soak_health._build_sample(NOW, snapshot, account)

    assert snapshot.drifted_accounts == ()
    assert snapshot.operational_alerts == ()
    assert sample["actual"] == WorkerState.ONLINE.value
    assert sample["healthy"] is True


@pytest.mark.parametrize(
    "actual_state",
    [
        WorkerState.SYNCING.value,
        WorkerState.RECONNECTING.value,
        WorkerState.NEEDS_VALIDATION.value,
        WorkerState.ERROR.value,
    ],
)
def test_soak_sample_rejects_non_online_canonical_states(actual_state: str) -> None:
    snapshot = _snapshot(actual_state=actual_state, aligned=False)
    sample = soak_health._build_sample(NOW, snapshot, snapshot.accounts[0])

    assert sample["healthy"] is False
    assert snapshot.drifted_accounts


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "health-online.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_soak_start_accepts_canonical_online(
    clean_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await domain_accounts.create_account("acc-online", enabled=True)
    snapshot = _snapshot()

    async def fake_snapshot(*, now=None):
        return snapshot

    async def skip_first_sample(_task_id: int) -> None:
        return None

    monkeypatch.setattr(soak_health, "build_runtime_snapshot", fake_snapshot)
    monkeypatch.setattr(soak_health, "sample_soak", skip_first_sample)

    run = await soak_health.start_soak(account_id="acc-online", hours=0.01)

    assert run.status == TaskStatus.RUNNING
    assert run.account_id == "acc-online"
    assert run.params["sample_count"] == 0
