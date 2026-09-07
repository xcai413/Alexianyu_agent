"""Durable Scheduler DueQueue contract shared by all supported databases."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from xianyu_agent.config import get_settings
from xianyu_agent.infrastructure.database.scheduler_queue import SqlAlchemyDueQueue


def _work_id(kind: str, backend: str) -> str:
    return f"sched-{kind}-{backend}-{uuid4().hex[:12]}"


async def _assert_due_and_stale_ownership(backend: str) -> None:
    queue = SqlAlchemyDueQueue()
    now = datetime.now(UTC)
    due_id = _work_id("due", backend)
    future_id = _work_id("future", backend)
    await queue.enqueue(work_id=due_id, payload={"kind": "due"}, available_at=now)
    await queue.enqueue(
        work_id=future_id,
        payload={"kind": "future"},
        available_at=now + timedelta(hours=1),
    )

    first = await queue.claim_due(
        now=now,
        lease_owner="worker-a",
        lease_duration=timedelta(seconds=5),
        limit=10,
    )
    due = next(work for work in first if work.work_id == due_id)
    assert all(work.work_id != future_id for work in first)
    assert due.payload == {"kind": "due"}

    assert await queue.claim_due(
        now=now,
        lease_owner="worker-b",
        lease_duration=timedelta(seconds=5),
        limit=10,
    ) == ()

    after_expiry = now + timedelta(seconds=6)
    second = await queue.claim_due(
        now=after_expiry,
        lease_owner="worker-b",
        lease_duration=timedelta(seconds=5),
        limit=10,
    )
    reclaimed = next(work for work in second if work.work_id == due_id)
    assert reclaimed.lease_token != due.lease_token
    assert await queue.complete(due, completed_at=after_expiry) is False
    assert await queue.complete(reclaimed, completed_at=after_expiry) is True


async def _assert_concurrent_claim_is_exclusive(backend: str) -> None:
    now = datetime.now(UTC)
    work_id = _work_id("race", backend)
    producer = SqlAlchemyDueQueue()
    await producer.enqueue(work_id=work_id, payload={"race": True}, available_at=now)

    left, right = await asyncio.gather(
        SqlAlchemyDueQueue().claim_due(
            now=now,
            lease_owner="worker-left",
            lease_duration=timedelta(minutes=1),
            limit=1,
        ),
        SqlAlchemyDueQueue().claim_due(
            now=now,
            lease_owner="worker-right",
            lease_duration=timedelta(minutes=1),
            limit=1,
        ),
    )

    claimed = (*left, *right)
    assert len(claimed) == 1
    assert claimed[0].work_id == work_id
    assert await producer.complete(claimed[0], completed_at=now) is True


async def _assert_restart_recovery(backend: str) -> None:
    now = datetime.now(UTC)
    work_id = _work_id("restart", backend)
    before_restart = SqlAlchemyDueQueue()
    await before_restart.enqueue(work_id=work_id, payload={"restart": True}, available_at=now)
    claimed = await before_restart.claim_due(
        now=now,
        lease_owner="dead-worker",
        lease_duration=timedelta(seconds=5),
        limit=1,
    )
    assert len(claimed) == 1

    after_restart = SqlAlchemyDueQueue()
    recovery_at = now + timedelta(seconds=6)
    assert await after_restart.recover_expired(now=recovery_at) == 1
    recovered = await after_restart.claim_due(
        now=recovery_at,
        lease_owner="replacement-worker",
        lease_duration=timedelta(minutes=1),
        limit=1,
    )
    assert len(recovered) == 1
    assert recovered[0].work_id == work_id
    assert recovered[0].lease_owner == "replacement-worker"
    assert await after_restart.complete(recovered[0], completed_at=recovery_at) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scheduler_due_queue_contract_across_backend() -> None:
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")

    assert get_settings().database_backend == backend
    await _assert_due_and_stale_ownership(backend)
    await _assert_concurrent_claim_is_exclusive(backend)
    await _assert_restart_recovery(backend)
