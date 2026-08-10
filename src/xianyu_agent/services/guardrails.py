"""Guardrails: safety gates for auto-reply and auto-delivery.

Rules:
  - message_rate: at most N inbound messages per account per hour before
    auto-reply is blocked (overwhelm / abuse protection)
  - quiet_hours: no auto-replies during a configured window (e.g. 23:00-08:00)
  - order_amount: reject delivery above a configured ceiling
  - circuit_breaker: after K consecutive send failures, pause auto-ops for the
    account (in-memory per process; tripping writes an audit event)

All blocks are recorded in audit_logs (action=guardrail_block) so the TUI and
any process can observe them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from xianyu_agent.config import get_settings
from xianyu_agent.db import AuditLog, get_async_session
from xianyu_agent.domain import messages as domain_messages
from xianyu_agent.utils.time_utils import format_local

logger = logging.getLogger(__name__)

GUARDRAIL_ACTION = "guardrail_block"


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    reason: str | None = None


class Guardrails:
    def __init__(
        self,
        *,
        max_msg_per_hour: int | None = None,
        max_order_amount: float | None = None,
        quiet_hours: str | None = None,
        fail_threshold: int | None = None,
    ) -> None:
        s = get_settings()
        self.max_msg_per_hour = (
            max_msg_per_hour if max_msg_per_hour is not None else s.guardrail_max_msg_per_hour
        )
        self.max_order_amount = (
            max_order_amount if max_order_amount is not None else s.guardrail_max_order_amount
        )
        self.quiet_hours = quiet_hours if quiet_hours is not None else s.guardrail_quiet_hours
        self.fail_threshold = (
            fail_threshold if fail_threshold is not None else s.guardrail_fail_threshold
        )
        self._fail_counts: dict[str, int] = {}

    # ---- message gates ----
    async def check_message(self, account_id: str, _content: str) -> GuardrailDecision:
        now = datetime.now(UTC)
        if self._in_quiet_hours(now):
            return GuardrailDecision(False, f"夜间静默时段({self.quiet_hours})")
        since = now - timedelta(hours=1)
        recent = await domain_messages.list_recent(
            account_id=account_id, since=since, direction="inbound", limit=10000
        )
        if len(recent) >= self.max_msg_per_hour:
            return GuardrailDecision(
                False, f"消息频率超限({len(recent)}/小时 > {self.max_msg_per_hour})"
            )
        return GuardrailDecision(True)

    # ---- delivery gates ----
    async def check_delivery(self, _account_id: str, amount: float) -> GuardrailDecision:
        if amount > self.max_order_amount:
            return GuardrailDecision(False, f"订单金额超限({amount} > {self.max_order_amount})")
        return GuardrailDecision(True)

    # ---- circuit breaker ----
    async def record_send_result(self, account_id: str, success: bool) -> GuardrailDecision | None:
        """Track consecutive send failures; returns a block decision when tripped."""
        if success:
            self._fail_counts[account_id] = 0
            return None
        n = self._fail_counts.get(account_id, 0) + 1
        self._fail_counts[account_id] = n
        if n >= self.fail_threshold:
            return GuardrailDecision(False, f"连续发送失败 {n} 次,熔断暂停")
        return None

    # ---- helpers ----
    def _in_quiet_hours(self, now: datetime) -> bool:
        start_s, _, end_s = self.quiet_hours.partition("-")
        try:
            start_min = int(start_s[:2]) * 60 + int(start_s[3:5])
            end_min = int(end_s[:2]) * 60 + int(end_s[3:5])
        except (ValueError, IndexError):
            return False
        now_min = now.hour * 60 + now.minute
        if start_min <= end_min:
            return start_min <= now_min < end_min
        return now_min >= start_min or now_min < end_min


async def write_guardrail_event(account_id: str, *, rule: str, detail: str) -> None:
    try:
        async with get_async_session() as session:
            session.add(
                AuditLog(
                    actor="system",
                    action=GUARDRAIL_ACTION,
                    target=account_id,
                    params={"rule": rule, "detail": detail},
                    result="blocked",
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("guardrail audit write failed: %s", exc)


async def recent_guardrail_events(*, limit: int = 10) -> list[dict]:
    async with get_async_session() as session:
        stmt = (
            select(AuditLog)
            .where(AuditLog.action == GUARDRAIL_ACTION)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "account_id": r.target,
            "rule": (r.params or {}).get("rule"),
            "detail": (r.params or {}).get("detail"),
            "at": format_local(r.created_at) or None,
        }
        for r in rows
    ]
