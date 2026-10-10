"""长登录续期的应用层编排:熔断期不续期,成功写回,失败转人工。

项目基准(Issue #2)允许这类正常生命周期维护,边界是"不以规避平台验证的方式强行续期",
因此这里有三条硬规则:

1. 账号处于人工验证冷却或熔断未解除时,一个请求都不发——续期本身也是平台请求,风控期间
   继续打正是参考项目记录过的"重试风暴让封控延长"。
2. 平台返回的增量 Cookie 即使出现在未完成响应中也先安全写回;业务结果仍保持失败并转人工。
   这与参考项目的 Cookie 合并策略一致,避免丢失服务端已经下发的有效状态。
3. 需要人工登录时如实上报(need_human_login),由上层通知人类,而不是反复尝试。

每次尝试都写审计:结果、模式、变更的 Cookie 名(只有名字,没有值)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from xianyu_agent.db import AuditLog, get_async_session
from xianyu_agent.domain.account import risk as account_risk
from xianyu_agent.protocol.mtop.signer import CookieSigner
from xianyu_agent.protocol.renew import RenewError, RenewOutcome, XianyuLongLoginRenewer

logger = logging.getLogger(__name__)

AUDIT_ACTION = "auth.long_login_renew"
ACTION_RENEWED = "renewed"
ACTION_SKIPPED = "skipped"
ACTION_FAILED = "failed"
ACTION_BLOCKED = "blocked"


@dataclass(frozen=True)
class RenewalReport:
    """Outcome of one renewal attempt, safe to print or audit."""

    account_id: str
    action: str
    success: bool = False
    mode: str | None = None
    changed_names: tuple[str, ...] = ()
    need_human_login: bool = False
    reason: str = ""
    cooldown_remaining_seconds: float | None = None


class LongLoginRenewalService:
    """Renew one account's long-login cookie, or explain why it did not."""

    def __init__(
        self,
        *,
        signer: CookieSigner | None = None,
        renewer: XianyuLongLoginRenewer | None = None,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._renewer = renewer or XianyuLongLoginRenewer()

    async def renew(
        self,
        account_id: str,
        *,
        now: datetime | None = None,
        session_expired: bool = False,
    ) -> RenewalReport:
        """Attempt one renewal; never retries and preserves safe partial Cookie updates."""
        circuit = await account_risk.get(account_id)
        if circuit is not None:
            return await self._blocked(account_id, circuit)
        cookie = await self._signer.load_cookie_value(account_id)
        if not cookie:
            return await self._failed(account_id, "账号没有可用 Cookie,需要人工重新登录", True)
        try:
            outcome = await self._renewer.renew(cookie, now=now, session_expired=session_expired)
        except RenewError as exc:
            logger.warning("long-login renewal failed type=%s", type(exc).__name__)
            return await self._failed(account_id, str(exc), True)
        return await self._apply(account_id, outcome)

    async def _apply(self, account_id: str, outcome: RenewOutcome) -> RenewalReport:
        """Persist confirmed or partial Cookie updates, then report the business result."""
        if outcome.success:
            saved = await self._signer.save_cookie(account_id, outcome.cookie)
            if not saved:
                return await self._failed(account_id, "续期成功但凭据写回失败", True)
            return await self._succeeded(account_id, outcome)
        if outcome.skipped:
            return await self._skipped(account_id, outcome)
        if outcome.cookie and outcome.changed_names:
            saved = await self._signer.save_cookie(account_id, outcome.cookie)
            if not saved:
                return await self._failed(account_id, "续期未完成且增量凭据写回失败", True)
        return await self._failed(account_id, outcome.message, outcome.need_human_login)

    async def _blocked(self, account_id: str, circuit: account_risk.WorkerRiskCircuit):
        cooling = circuit.is_cooling()
        remaining = circuit.remaining_seconds() if cooling else None
        reason = (
            f"账号处于人工验证冷却(约剩 {int((remaining or 0) // 60) + 1} 分钟),按规则不发起续期"
            if cooling
            else "账号的人工验证熔断尚未解除,需先完成人工验证与一次成功刷新"
        )
        report = RenewalReport(
            account_id=account_id,
            action=ACTION_BLOCKED,
            need_human_login=True,
            reason=reason,
            cooldown_remaining_seconds=remaining,
        )
        await _audit(report)
        return report

    async def _succeeded(self, account_id: str, outcome: RenewOutcome) -> RenewalReport:
        report = RenewalReport(
            account_id=account_id,
            action=ACTION_RENEWED,
            success=True,
            mode=outcome.mode,
            changed_names=outcome.changed_names,
            reason=outcome.message,
        )
        await _audit(report)
        return report

    async def _skipped(self, account_id: str, outcome: RenewOutcome) -> RenewalReport:
        report = RenewalReport(
            account_id=account_id,
            action=ACTION_SKIPPED,
            mode=outcome.mode,
            need_human_login=outcome.need_human_login,
            reason=outcome.message,
        )
        await _audit(report)
        return report

    async def _failed(self, account_id: str, reason: str, need_human_login: bool) -> RenewalReport:
        report = RenewalReport(
            account_id=account_id,
            action=ACTION_FAILED,
            need_human_login=need_human_login,
            reason=reason,
        )
        await _audit(report)
        return report


async def _audit(report: RenewalReport) -> None:
    """Append one audit row; names only, never cookie or token values."""
    params: dict[str, object] = {"reason": report.reason}
    if report.mode:
        params["mode"] = report.mode
    if report.changed_names:
        params["changed_names"] = list(report.changed_names)
    try:
        async with get_async_session() as session:
            session.add(
                AuditLog(
                    actor="system",
                    action=AUDIT_ACTION,
                    target=report.account_id,
                    params=params,
                    result=report.action,
                    error=report.reason if report.action == ACTION_FAILED else None,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("renewal audit write failed: %s", type(exc).__name__)
