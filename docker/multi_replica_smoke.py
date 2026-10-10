"""双副本容器栈的无真实账号验收探针。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.request import urlopen

from sqlalchemy import delete, select

from xianyu_agent.application.ports.outbox import OutboxEvent
from xianyu_agent.db import Account, AuditLog, RuntimeLease, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.account import lease as account_lease
from xianyu_agent.domain.runtime import daemon as daemon_domain, lease as runtime_lease
from xianyu_agent.infrastructure.database.models import TransactionalOutbox
from xianyu_agent.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


def _probe(name: str) -> dict[str, object]:
    with urlopen(f"http://{name}:8089/health", timeout=5) as response:
        payload = json.load(response)
    if response.status != 200 or payload.get("status") != "healthy":
        raise RuntimeError(f"{name} health failed: {payload}")
    return payload


async def _claim_outbox(event_id: str, *, owner: str, now: datetime) -> bool:
    async with SqlAlchemyUnitOfWork() as uow:
        rows = await uow.outbox.claim_pending(
            owner=owner,
            lease_duration=timedelta(seconds=30),
            limit=100,
            now=now,
        )
        await uow.commit()
    return any(row.event_id == event_id for row in rows)


async def _verify_database_contracts() -> dict[str, object]:
    now = datetime.now(UTC)
    async with get_async_session() as session:
        await session.execute(delete(AuditLog).where(AuditLog.target.like("m5-smoke-%")))
        await session.execute(delete(Account).where(Account.account_id.like("m5-smoke-%")))
        await session.execute(delete(RuntimeLease).where(RuntimeLease.name.like("m5-smoke-%")))
        await session.commit()
    instances = await daemon_domain.list_instances(limit=20)
    active = [
        row
        for row in instances
        if row.status == "running" and row.lease_until is not None and row.lease_until > now
    ]
    if len(active) != 2:
        raise RuntimeError(f"expected 2 active daemon leases, got {len(active)}")

    suffix = uuid.uuid4().hex[:12]
    account_id = f"m5-smoke-{suffix}"
    await domain_accounts.create_account(account_id, enabled=False)
    winners = await asyncio.gather(
        account_lease.claim(account_id, instance_id=active[0].instance_id, now=now),
        account_lease.claim(account_id, instance_id=active[1].instance_id, now=now),
    )
    if sum(winners) != 1:
        raise RuntimeError(f"account lease winners={winners}")

    lease_name = f"m5-smoke-{suffix}"
    sampler_winners = await asyncio.gather(
        runtime_lease.claim(
            lease_name,
            owner_id=active[0].instance_id,
            ttl=timedelta(seconds=30),
            now=now,
        ),
        runtime_lease.claim(
            lease_name,
            owner_id=active[1].instance_id,
            ttl=timedelta(seconds=30),
            now=now,
        ),
    )
    if sum(sampler_winners) != 1:
        raise RuntimeError(f"runtime lease winners={sampler_winners}")

    event_id = f"m5-smoke-{suffix}"
    async with SqlAlchemyUnitOfWork() as uow:
        await uow.outbox.enqueue(
            OutboxEvent(
                event_id=event_id,
                event_type="M5SmokeEvent",
                version=1,
                correlation_id=f"corr-{suffix}",
                payload={"smoke": True},
                occurred_at=now,
            )
        )
        await uow.commit()
    outbox_winners = await asyncio.gather(
        _claim_outbox(event_id, owner=active[0].instance_id, now=now),
        _claim_outbox(event_id, owner=active[1].instance_id, now=now),
    )
    if sum(outbox_winners) != 1:
        raise RuntimeError(f"outbox lease winners={outbox_winners}")

    async with get_async_session() as session:
        account = await session.scalar(select(Account).where(Account.account_id == account_id))
        owner = account.owner_instance_id if account is not None else None
        await session.execute(delete(AuditLog).where(AuditLog.target == account_id))
        await session.execute(delete(Account).where(Account.account_id == account_id))
        await session.execute(delete(RuntimeLease).where(RuntimeLease.name == lease_name))
        await session.execute(
            delete(TransactionalOutbox).where(TransactionalOutbox.event_id == event_id)
        )
        await session.commit()

    return {
        "active_daemons": sorted(row.instance_id for row in active),
        # This is the transient m5-smoke-* account created above, never an
        # operator's configured account. Keep the distinction explicit in the
        # probe output so an operator cannot mistake it for a runtime owner.
        "smoke_account_owner": owner,
        "account_lease_winners": sum(winners),
        "runtime_lease_winners": sum(sampler_winners),
        "outbox_lease_winners": sum(outbox_winners),
    }


async def main() -> None:
    result = {
        "app_a": _probe("app-a"),
        "app_b": _probe("app-b"),
        "database": await _verify_database_contracts(),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
