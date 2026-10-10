"""Marker coverage for platform risk control detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.account import risk


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "risk_markers.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.parametrize(
    "message",
    [
        "FAIL_SYS_USER_VALIDATE",
        "RGV587_ERROR::SM",
        "FAIL_SYS_FLOW_LIMIT",
        "哎哟喂,被挤爆啦,请稍后重试",
        "SM::something",
        "商品详情平台返回失败(FAIL_SYS_USER_VALIDATE,RGV587_ERROR::SM::哎哟喂,被挤爆啦)",
    ],
)
def test_risk_family_is_detected(message: str) -> None:
    assert risk.is_user_validate_error(message) is True
    assert risk.is_user_validate_error(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "账号 acc-1 Cookie 已失效,请重新扫码登录",
        "商品列表返回非 JSON(HTTP 502)",
        "续期网络请求失败(ConnectError)",
        "",
    ],
)
def test_ordinary_errors_are_not_mistaken_for_risk_control(message: str) -> None:
    assert risk.is_user_validate_error(message) is False


def test_detected_code_reports_the_first_marker() -> None:
    assert risk.detected_risk_code("RGV587_ERROR::SM") == "RGV587_ERROR"
    assert risk.detected_risk_code("FAIL_SYS_FLOW_LIMIT") == "FAIL_SYS_FLOW_LIMIT"
    assert (
        risk.detected_risk_code("FAIL_SYS_USER_VALIDATE,RGV587_ERROR") == "FAIL_SYS_USER_VALIDATE"
    )
    assert risk.detected_risk_code("nothing here") == risk.USER_VALIDATE_CODE


def test_cooldown_escalates_with_consecutive_hits() -> None:
    assert risk.cooldown_for(1) == timedelta(seconds=60)
    assert risk.cooldown_for(2) == timedelta(seconds=180)
    assert risk.cooldown_for(3) == timedelta(seconds=300)
    assert risk.cooldown_for(4) == timedelta(seconds=600)
    assert risk.cooldown_for(5) == risk.USER_VALIDATE_COOLDOWN
    assert risk.cooldown_for(9) == risk.USER_VALIDATE_COOLDOWN
    assert risk.cooldown_for(0) == timedelta(seconds=60)


@pytest.mark.asyncio
async def test_repeated_hits_escalate_and_recovery_resets_the_streak(clean_db) -> None:
    now = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
    first = await risk.open_user_validate("acc-1", now=now)
    second = await risk.open_user_validate("acc-1", now=now + timedelta(minutes=2))
    third = await risk.open_user_validate("acc-1", now=now + timedelta(minutes=6))

    assert first is not None
    assert first.cooldown_until == now + timedelta(seconds=60)
    assert second is not None
    assert second.cooldown_until == now + timedelta(minutes=2, seconds=180)
    assert third is not None
    assert third.cooldown_until == now + timedelta(minutes=6, seconds=300)

    current = await risk.get("acc-1")
    cleared = await risk.clear_after_refresh("acc-1", expected_revision=current.revision)
    assert cleared is True
    assert await risk.get("acc-1") is None

    again = await risk.open_user_validate("acc-1", now=now + timedelta(hours=1))
    assert again is not None
    assert again.cooldown_until == now + timedelta(hours=1, seconds=60)


@pytest.mark.asyncio
async def test_circuit_records_the_marker_that_was_actually_seen(clean_db) -> None:
    circuit = await risk.open_user_validate("acc-1", code="FAIL_SYS_FLOW_LIMIT")

    assert circuit is not None
    assert circuit.code == "FAIL_SYS_FLOW_LIMIT"
    stored = await risk.get("acc-1")
    assert stored is not None
    assert stored.code == "FAIL_SYS_FLOW_LIMIT"
    assert stored.is_cooling() is True


@pytest.mark.asyncio
async def test_circuit_defaults_to_the_validate_code(clean_db) -> None:
    circuit = await risk.open_user_validate("acc-1")

    assert circuit is not None
    assert circuit.code == risk.USER_VALIDATE_CODE
