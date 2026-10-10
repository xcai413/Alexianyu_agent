"""Tests for per-account platform request pacing."""

from __future__ import annotations

import pytest

from xianyu_agent.application.pacing import (
    AccountPacer,
    get_pacer,
    reset_pacer,
)
from xianyu_agent.config import reset_settings_cache


class FakeClock:
    """Monotonic clock advanced only by the pacer's own sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def build_pacer(
    *, rate_per_minute: int = 60, jitter_ratio: float = 0.0, random_value: float = 0.0
) -> tuple[AccountPacer, FakeClock]:
    clock = FakeClock()
    pacer = AccountPacer(
        rate_per_minute=rate_per_minute,
        jitter_ratio=jitter_ratio,
        clock=clock,
        sleep=clock.sleep,
        random_source=lambda: random_value,
    )
    return pacer, clock


async def test_quota_is_free_and_burst_never_sleeps() -> None:
    pacer, clock = build_pacer(rate_per_minute=60)

    waited = [await pacer.acquire("acc-1") for _ in range(60)]

    assert waited == [0.0] * 60
    assert clock.slept == []


async def test_over_quota_waits_one_refill_interval() -> None:
    pacer, clock = build_pacer(rate_per_minute=60)

    for _ in range(60):
        await pacer.acquire("acc-1")
    waited = await pacer.acquire("acc-1")

    # 60 次/分钟 = 每秒 1 个令牌;桶空后必须等满一个令牌的生成时间。
    assert waited == pytest.approx(1.0)
    assert clock.slept == pytest.approx([1.0])


async def test_wait_is_jittered_within_bounds() -> None:
    low, low_clock = build_pacer(rate_per_minute=60, jitter_ratio=0.3, random_value=0.0)
    high, high_clock = build_pacer(rate_per_minute=60, jitter_ratio=0.3, random_value=1.0)
    for pacer in (low, high):
        for _ in range(60):
            await pacer.acquire("acc-1")

    shortest = await low.acquire("acc-1")
    longest = await high.acquire("acc-1")

    assert low_clock.slept == pytest.approx([1.0])
    assert high_clock.slept == pytest.approx([1.3])
    assert shortest < longest


async def test_bucket_refills_as_time_passes() -> None:
    pacer, clock = build_pacer(rate_per_minute=60)
    for _ in range(60):
        await pacer.acquire("acc-1")

    clock.now += 60.0
    waited = await pacer.acquire("acc-1")

    assert waited == 0.0
    assert clock.slept == []


async def test_each_account_has_its_own_bucket() -> None:
    pacer, clock = build_pacer(rate_per_minute=60)
    for _ in range(61):
        await pacer.acquire("acc-1")

    waited = await pacer.acquire("acc-2")

    assert waited == 0.0
    assert len(clock.slept) == 1


async def test_forget_drops_the_bucket() -> None:
    pacer, clock = build_pacer(rate_per_minute=60)
    for _ in range(61):
        await pacer.acquire("acc-1")

    pacer.forget("acc-1")
    clock.slept.clear()
    waited = await pacer.acquire("acc-1")

    assert waited == 0.0
    assert clock.slept == []


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError, match="rate_per_minute"):
        AccountPacer(rate_per_minute=0)
    with pytest.raises(ValueError, match="jitter_ratio"):
        AccountPacer(jitter_ratio=1.5)
    with pytest.raises(ValueError, match="jitter_ratio"):
        AccountPacer(jitter_ratio=-0.1)


def test_shared_pacer_reads_settings_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XIANYU_PLATFORM_RATE_PER_MINUTE", "120")
    monkeypatch.setenv("XIANYU_PLATFORM_JITTER_RATIO", "0.5")
    reset_settings_cache()
    reset_pacer()
    try:
        pacer = get_pacer()

        assert pacer is get_pacer()
        assert pacer.rate_per_minute == 120
        assert pacer.jitter_ratio == pytest.approx(0.5)

        reset_pacer()

        assert get_pacer() is not pacer
    finally:
        reset_settings_cache()
        reset_pacer()


def test_pacer_defaults_match_documented_quota() -> None:
    pacer = AccountPacer()

    assert pacer.rate_per_minute == 30
    assert pacer.jitter_ratio == pytest.approx(0.3)
