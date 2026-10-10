"""M2:账号归属租约与按 owner 分片。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.account import lease as account_lease, risk as worker_risk
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.runtime.account_pool import AccountPool


@pytest.fixture
async def lease_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "lease.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _audit_actions() -> list[str]:
    async with db_mod.async_session_factory() as session:
        rows = (
            (await session.execute(select(AuditLog.action).order_by(AuditLog.id))).scalars().all()
        )
    return list(rows)


async def test_claim_renew_release_roundtrip(lease_db: Path) -> None:
    await domain_accounts.create_account("acc-1")
    now = datetime.now(UTC)

    assert await account_lease.live_owner("acc-1") is None
    assert await account_lease.claim("acc-1", instance_id="replica-a", now=now) is True

    lease = await account_lease.get("acc-1")
    assert lease is not None
    assert lease.owner_instance_id == "replica-a"
    assert lease.is_live(now=now)
    assert await account_lease.live_owner("acc-1") == "replica-a"

    # 第二个副本在租约有效期内抢不到
    assert await account_lease.claim("acc-1", instance_id="replica-b", now=now) is False
    # 只有 owner 能续租
    assert await account_lease.renew("acc-1", instance_id="replica-b", now=now) is False
    assert await account_lease.renew("acc-1", instance_id="replica-a", now=now) is True
    assert await account_lease.renew_owned("replica-a", now=now) == 1

    assert await account_lease.release("acc-1", instance_id="replica-a") is True
    assert await account_lease.live_owner("acc-1") is None
    assert await account_lease.release("acc-1", instance_id="replica-a") is False

    actions = await _audit_actions()
    assert "account_lease_acquired" in actions
    assert "account_lease_released" in actions


async def test_release_owned_clears_only_graceful_owner_accounts(lease_db: Path) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_accounts.create_account("acc-2")
    await domain_accounts.create_account("acc-3")
    assert await account_lease.claim("acc-1", instance_id="replica-a")
    assert await account_lease.claim("acc-2", instance_id="replica-a")
    assert await account_lease.claim("acc-3", instance_id="replica-b")

    assert await account_lease.release_owned("replica-a") == 2

    assert (await account_lease.get("acc-1")).owner_instance_id is None
    assert (await account_lease.get("acc-2")).owner_instance_id is None
    assert (await account_lease.get("acc-3")).owner_instance_id == "replica-b"


async def test_takeover_requires_expired_lease_and_cooldown(lease_db: Path) -> None:
    await domain_accounts.create_account("acc-1")
    now = datetime.now(UTC)
    ttl = timedelta(seconds=30)
    cooldown = timedelta(seconds=60)
    assert await account_lease.claim("acc-1", instance_id="replica-a", ttl=ttl, now=now) is True

    # 刚过期还不行:要等过冷却窗口,避免抖动的双会话
    just_expired = now + ttl + timedelta(seconds=1)
    assert (
        await account_lease.claim(
            "acc-1", instance_id="replica-b", cooldown=cooldown, now=just_expired
        )
        is False
    )
    assert await account_lease.live_owner("acc-1", now=just_expired) is None

    after_cooldown = now + ttl + cooldown + timedelta(seconds=1)
    assert (
        await account_lease.claim(
            "acc-1", instance_id="replica-b", cooldown=cooldown, now=after_cooldown
        )
        is True
    )
    lease = await account_lease.get("acc-1")
    assert lease is not None
    assert lease.owner_instance_id == "replica-b"


async def test_takeover_approval_gate_is_shared_and_consumed_on_success(
    lease_db: Path,
) -> None:
    await domain_accounts.create_account("acc-1")
    now = datetime.now(UTC)
    ttl = timedelta(seconds=5)
    cooldown = timedelta(seconds=10)
    assert await account_lease.claim("acc-1", instance_id="replica-a", ttl=ttl, now=now)
    takeover_at = now + ttl + cooldown + timedelta(seconds=1)

    blocked = await account_lease.claim_for_start(
        "acc-1",
        instance_id="replica-b",
        cooldown=cooldown,
        require_approval=True,
        now=takeover_at,
    )
    assert blocked.acquired is False
    assert blocked.reason == "takeover_approval_required"
    assert await account_lease.approve_takeover("acc-1", actor="operator") is True

    claimed = await account_lease.claim_for_start(
        "acc-1",
        instance_id="replica-b",
        cooldown=cooldown,
        require_approval=True,
        now=takeover_at,
    )
    assert claimed.acquired is True
    assert claimed.takeover is True
    assert claimed.previous_owner == "replica-a"
    assert await account_lease.record_takeover_success("acc-1", instance_id="replica-b")
    lease = await account_lease.get("acc-1")
    assert lease is not None
    assert lease.takeover_approved_at is None
    assert lease.takeover_failure_count == 0
    actions = await _audit_actions()
    assert "account_takeover_approved" in actions
    assert "account_takeover_succeeded" in actions


class _FakeWorker:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.state = WorkerState.DISABLED
        self.started = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False


class _TakeoverWorker:
    should_fail = False

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.state = WorkerState.DISABLED

    def start(self) -> asyncio.Task[None]:
        async def _start() -> None:
            if self.should_fail:
                self.state = WorkerState.ERROR
                raise RuntimeError("fake transport failed")
            self.state = WorkerState.ONLINE

        return asyncio.create_task(_start())

    async def stop(self) -> None:
        self.state = WorkerState.DISABLED


async def _expire_owner(account_id: str) -> None:
    now = datetime.now(UTC) - account_lease.ACCOUNT_TAKEOVER_COOLDOWN - timedelta(minutes=1)
    assert await account_lease.claim(
        account_id,
        instance_id="dead-replica",
        ttl=timedelta(seconds=1),
        now=now,
    )


async def _finish_pool_startup(pool: AccountPool) -> None:
    if pool._startup_tasks:
        await asyncio.gather(*tuple(pool._startup_tasks.values()), return_exceptions=True)
    await asyncio.sleep(0)
    await pool._drain_takeover_result_tasks()


async def test_pool_takeover_success_with_fake_transport(lease_db: Path) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_accounts.set_desired_state("acc-1", "running")
    await _expire_owner("acc-1")
    _TakeoverWorker.should_fail = False
    pool = AccountPool(
        worker_factory=_TakeoverWorker,
        instance_id="replica-b",
        takeover_require_approval=False,
    )

    result = await pool.reconcile_desired_accounts()
    await _finish_pool_startup(pool)

    assert result["started"] == ["acc-1"]
    lease = await account_lease.get("acc-1")
    assert lease is not None
    assert lease.owner_instance_id == "replica-b"
    assert lease.takeover_failure_count == 0
    assert "account_takeover_succeeded" in await _audit_actions()


async def test_pool_takeover_failures_trip_persisted_risk_circuit(lease_db: Path) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_accounts.set_desired_state("acc-1", "running")
    await _expire_owner("acc-1")
    _TakeoverWorker.should_fail = True

    for index in range(3):
        pool = AccountPool(
            worker_factory=_TakeoverWorker,
            instance_id=f"replica-{index}",
            takeover_require_approval=False,
            takeover_max_failures=3,
        )
        result = await pool.reconcile_desired_accounts()
        assert result["started"] == ["acc-1"]
        await _finish_pool_startup(pool)

    lease = await account_lease.get("acc-1")
    account = await domain_accounts.get_account("acc-1")
    status = await domain_accounts.worker_status_for("acc-1")
    assert lease is not None
    assert account is not None
    assert status is not None
    assert lease.owner_instance_id is None
    assert lease.takeover_failure_count == 3
    assert account.desired_state == "stopped"
    assert status.risk_code == account_lease.TAKEOVER_FAILURE_LIMIT_CODE
    assert status.risk_recovery_required is True

    current = await worker_risk.get("acc-1")
    assert (
        await worker_risk.clear_after_refresh("acc-1", expected_revision=current.revision) is True
    )
    lease = await account_lease.get("acc-1")
    assert lease is not None
    assert lease.takeover_failure_count == 0


async def test_pool_reconcile_only_starts_owned_accounts(lease_db: Path) -> None:
    await domain_accounts.create_account("acc-1")
    await domain_accounts.set_desired_state("acc-1", "running")

    first = AccountPool(worker_factory=_FakeWorker, instance_id="replica-a")
    started_first = await first.reconcile_desired_accounts()

    assert started_first["started"] == ["acc-1"]
    assert (await account_lease.live_owner("acc-1")) == "replica-a"

    # 第二个副本看到的是别人持有的账号:不启动 Worker,也不覆盖归属。
    second = AccountPool(worker_factory=_FakeWorker, instance_id="replica-b")
    started_second = await second.reconcile_desired_accounts()

    assert started_second["started"] == []
    assert "acc-1" not in second.account_ids
    assert (await account_lease.get("acc-1")).owner_instance_id == "replica-a"
