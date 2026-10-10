"""长登录的提前刷新与周期调度。

无人值守的关键不是"断了再救",而是到期前主动续期。这里做两件事:

1. 判断某个账号现在是否该续期:读长登录 Cookie 的到期时间,提前一个 lead_time 触发;
   处于 SDK 疲劳窗口时不发起(sdkSilent 未过期说明刚续过,再打就是重试风暴);
   长登录已过期时仍然标记为需要处理,让上层产生人工登录提醒而不是静默等待。
2. 周期驱动多个账号:单账号失败只记该账号的结果,不影响其他账号;同一账号两次尝试之间
   保留最小间隔,避免某个失败账号被反复重试。

真正的"发不发请求"由 LongLoginRenewalService 决定(账号处于人工验证熔断时一个请求都不发)。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from xianyu_agent.protocol.mtop.signer import CookieSigner
from xianyu_agent.protocol.renew import (
    COOKIE3_EXPIRY_COOKIE,
    FATIGUE_COOKIE,
    HAVANA_EXPIRY_COOKIE,
    javascript_millis,
    parse_cookie_values,
    strict_cookie_time_after,
)

from .renewal import LongLoginRenewalService, RenewalReport

logger = logging.getLogger(__name__)

DEFAULT_LEAD_TIME = timedelta(hours=12)
DEFAULT_MIN_INTERVAL = timedelta(minutes=30)

CookieReader = Callable[[str], Awaitable[str]]
AccountLister = Callable[[], Awaitable[Sequence[str]]]


@dataclass(frozen=True)
class RenewalDecision:
    """Whether one account should be renewed now, and why."""

    account_id: str
    due: bool
    reason: str
    seconds_until_expiry: float | None = None


def renewal_due(
    account_id: str,
    cookie: str,
    *,
    now: datetime,
    lead_time: timedelta = DEFAULT_LEAD_TIME,
) -> RenewalDecision:
    """Decide whether this account needs a renewal attempt right now."""
    if not cookie.strip():
        return RenewalDecision(account_id, True, "账号没有可用 Cookie,需要人工重新登录")
    values = parse_cookie_values(cookie)
    if strict_cookie_time_after(values.get(FATIGUE_COOKIE), now):
        return RenewalDecision(account_id, False, "处于 SDK 疲劳窗口,跳过本次续期")
    expiries = [
        javascript_millis(values.get(name))
        for name in (HAVANA_EXPIRY_COOKIE, COOKIE3_EXPIRY_COOKIE)
    ]
    live = [value for value in expiries if not math.isnan(value)]
    now_ms = now.timestamp() * 1000
    if not live:
        return RenewalDecision(account_id, True, "长登录凭证已缺失或不可解析,需要人工重新登录")
    remaining_ms = max(live) - now_ms
    remaining = remaining_ms / 1000
    if remaining <= 0:
        return RenewalDecision(
            account_id, True, "长登录已过期,需要人工重新登录", seconds_until_expiry=remaining
        )
    if remaining <= lead_time.total_seconds():
        hours = max(remaining / 3600, 0)
        return RenewalDecision(
            account_id,
            True,
            f"长登录将在 {hours:.1f} 小时内到期,提前续期",
            seconds_until_expiry=remaining,
        )
    return RenewalDecision(
        account_id, False, "长登录尚未进入提前续期窗口", seconds_until_expiry=remaining
    )


class LongLoginRenewalScheduler:
    """Run renewal for every account that is due, one account at a time."""

    def __init__(
        self,
        *,
        service: LongLoginRenewalService | None = None,
        cookie_reader: CookieReader | None = None,
        account_lister: AccountLister | None = None,
        lead_time: timedelta = DEFAULT_LEAD_TIME,
        min_interval: timedelta = DEFAULT_MIN_INTERVAL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service or LongLoginRenewalService()
        signer = CookieSigner()
        self._read_cookie = cookie_reader or signer.load_cookie_value
        self._list_accounts = account_lister
        self._lead_time = lead_time
        self._min_interval = min_interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_attempt: dict[str, datetime] = {}

    async def run_once(
        self,
        account_ids: Sequence[str] | None = None,
        *,
        force: bool = False,
    ) -> tuple[RenewalReport, ...]:
        """Renew due accounts; force is the bounded periodic maintenance pass."""
        accounts = list(account_ids) if account_ids is not None else await self._list()
        reports: list[RenewalReport] = []
        for account_id in accounts:
            try:
                report = await self._renew_if_due(account_id, force=force)
            except Exception as exc:
                logger.warning("renewal scheduling failed for one account: %s", type(exc).__name__)
                continue
            if report is not None:
                reports.append(report)
        return tuple(reports)

    async def _renew_if_due(self, account_id: str, *, force: bool = False) -> RenewalReport | None:
        now = self._clock()
        last = self._last_attempt.get(account_id)
        if last is not None and now - last < self._min_interval:
            return None
        cookie = await self._read_cookie(account_id)
        decision = renewal_due(account_id, cookie, now=now, lead_time=self._lead_time)
        if not decision.due and not force:
            logger.debug("renewal not due: %s", decision.reason)
            return None
        self._last_attempt[account_id] = now
        logger.info("long-login renewal due for account=%s", account_id)
        return await self._service.renew(account_id, now=now)

    async def _list(self) -> Sequence[str]:
        if self._list_accounts is None:
            msg = "未提供账号来源,无法周期续期"
            raise ValueError(msg)
        return await self._list_accounts()
