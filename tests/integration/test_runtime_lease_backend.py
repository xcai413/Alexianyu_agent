"""runtime_leases 在 SQLite/MySQL/PostgreSQL 上的 CAS 契约。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from xianyu_agent.config import get_settings
from xianyu_agent.domain.runtime import lease as runtime_lease


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_lease_contract_across_backend() -> None:
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend
    now = datetime.now(UTC)
    ttl = timedelta(seconds=30)

    assert await runtime_lease.claim(
        "backend-contract",
        owner_id="replica-a",
        ttl=ttl,
        allow_owner_renewal=False,
        now=now,
    )
    assert not await runtime_lease.claim(
        "backend-contract",
        owner_id="replica-b",
        ttl=ttl,
        allow_owner_renewal=False,
        now=now + timedelta(seconds=29),
    )
    assert await runtime_lease.claim(
        "backend-contract",
        owner_id="replica-b",
        ttl=ttl,
        allow_owner_renewal=False,
        now=now + timedelta(seconds=31),
    )
