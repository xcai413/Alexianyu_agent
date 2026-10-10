"""平台请求的每账号节流与抖动。

参考项目的实测教训:账号被判定异常后继续按原节奏重试,风控不会自行解除,反而因为持续
请求一直延长(它们记录过每分钟数百次请求、两小时未恢复)。批量铺货逐行发布、同步翻页、
账号续期都会在短时间发出多次平台请求,因此这里给每个账号一个令牌桶:配额内直接放行,
超出配额才等待,并在等待时间上加入抖动,避免多账号同时重试形成同步冲击。

桶是进程内的:daemon 是单进程,这已经够用;跨进程不需要共享节流状态(不同进程本来也不该
同时操作同一账号,那由账号锁负责)。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from xianyu_agent.config import get_settings

DEFAULT_RATE_PER_MINUTE = 30
DEFAULT_JITTER_RATIO = 0.3


class AccountPacer:
    """Token bucket per account, with jittered waits."""

    def __init__(
        self,
        *,
        rate_per_minute: int = DEFAULT_RATE_PER_MINUTE,
        jitter_ratio: float = DEFAULT_JITTER_RATIO,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> None:
        if rate_per_minute < 1:
            msg = "rate_per_minute 至少为 1"
            raise ValueError(msg)
        if not 0 <= jitter_ratio <= 1:
            msg = "jitter_ratio 必须在 0 到 1 之间"
            raise ValueError(msg)
        self._rate = float(rate_per_minute)
        self._jitter = jitter_ratio
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._random = random_source or random.random
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}

    @property
    def rate_per_minute(self) -> int:
        return int(self._rate)

    @property
    def jitter_ratio(self) -> float:
        return self._jitter

    def _refill(self, account_id: str, now: float) -> float:
        tokens = self._tokens.get(account_id, self._rate)
        last = self._last_refill.get(account_id, now)
        tokens = min(self._rate, tokens + max(now - last, 0.0) * (self._rate / 60.0))
        self._tokens[account_id] = tokens
        self._last_refill[account_id] = now
        return tokens

    async def acquire(self, account_id: str) -> float:
        """Take one token, waiting when the bucket is empty; returns waited seconds."""
        now = self._clock()
        tokens = self._refill(account_id, now)
        if tokens >= 1:
            self._tokens[account_id] = tokens - 1
            return 0.0
        deficit = (1 - tokens) * (60.0 / self._rate)
        wait_for = deficit * (1 + self._jitter * self._random())
        await self._sleep(wait_for)
        finished = self._clock()
        self._tokens[account_id] = self._refill(account_id, finished) - 1
        return wait_for

    def forget(self, account_id: str) -> None:
        """Drop one account's bucket, e.g. after the account is stopped."""
        self._tokens.pop(account_id, None)
        self._last_refill.pop(account_id, None)


_default_pacer: AccountPacer | None = None


def get_pacer() -> AccountPacer:
    """Shared pacer built from settings; services default to this instance."""
    global _default_pacer  # noqa: PLW0603 - 进程内单例,与 get_settings 同风格
    if _default_pacer is None:
        settings = get_settings()
        _default_pacer = AccountPacer(
            rate_per_minute=settings.platform_rate_per_minute,
            jitter_ratio=settings.platform_jitter_ratio,
        )
    return _default_pacer


def reset_pacer() -> None:
    """Test hook: drop the shared pacer so new settings take effect."""
    global _default_pacer  # noqa: PLW0603 - 与 get_settings 的测试钩子同风格
    _default_pacer = None
