"""M4 集群级单例任务租约与 soak sampler 单选。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xianyu_agent.application.health import soak
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain.runtime import lease as runtime_lease


@pytest.fixture
async def runtime_lease_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "runtime-lease.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def test_single_shot_lease_blocks_every_owner_until_expiry(runtime_lease_db) -> None:
    _ = runtime_lease_db
    now = datetime(2026, 9, 20, 1, 0, tzinfo=UTC)
    ttl = timedelta(seconds=55)

    assert await runtime_lease.claim(
        "sampler",
        owner_id="replica-a",
        ttl=ttl,
        allow_owner_renewal=False,
        now=now,
    )
    assert not await runtime_lease.claim(
        "sampler",
        owner_id="replica-a",
        ttl=ttl,
        allow_owner_renewal=False,
        now=now + timedelta(seconds=1),
    )
    assert not await runtime_lease.claim(
        "sampler",
        owner_id="replica-b",
        ttl=ttl,
        allow_owner_renewal=False,
        now=now + timedelta(seconds=54),
    )
    assert await runtime_lease.claim(
        "sampler",
        owner_id="replica-b",
        ttl=ttl,
        allow_owner_renewal=False,
        now=now + timedelta(seconds=56),
    )


async def test_sample_active_soaks_skips_when_sampler_lease_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(soak.runtime_lease, "claim", denied)

    assert await soak.sample_active_soaks(sampler_id="replica-b") == 0
