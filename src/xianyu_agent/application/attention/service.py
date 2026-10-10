"""待人工处理队列(对照 Helper 的 notifications/outbox/uncertain 与
butler 的日志/风险页)。

设计取舍:这里只负责把"必须由人决定"的事实聚合成一个可查询清单,不自己发通知。原因是我们
已经有稳定的事实来源(发货任务、自动化执行、账号风控),再复制一份通知状态只会产生第二个
需要对齐的真相。清单里的每一项都带"下一步该跑什么命令",让运营不需要读代码就能收尾;
要不要推到钉钉/飞书等渠道由 `application/notify` 决定,推送事实记在 `alert_dispatches`。

清单只读:它不修改任何业务状态,也不触发平台动作。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from xianyu_agent.db import (
    Account,
    Message,
    MessageFilterRule,
    PlatformAutomationRun,
    get_async_session,
)
from xianyu_agent.domain.delivery import DeliveryJobState
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.infrastructure.database.models import WorkerStatus as WorkerStatusRow
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyDeliveryJobRepository

# 发货任务的"需要人"状态与 domain 的 DeliveryJob.requires_human 保持一致。
HUMAN_JOB_STATES: tuple[str, ...] = (
    DeliveryJobState.UNCERTAIN.value,
    DeliveryJobState.NEEDS_CONFIRMATION.value,
    DeliveryJobState.BLOCKED.value,
)

KIND_DELIVERY_JOB = "delivery_job"
KIND_AUTOMATION_RUN = "automation_run"
KIND_ACCOUNT_RISK = "account_risk"
KIND_MESSAGE_RECEIVED = "message_received"


@dataclass(frozen=True, slots=True)
class AttentionItem:
    """One fact that needs a human decision, with the command that resolves it."""

    kind: str
    target: str
    state: str
    reason: str
    next_command: str
    account_id: str | None = None
    since: datetime | None = None


class AttentionService:
    """Read-only aggregation of everything waiting on a human."""

    async def list_items(
        self,
        *,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[AttentionItem]:
        if not 1 <= limit <= 1000:
            msg = "limit 必须在 1 到 1000 之间"
            raise ValueError(msg)
        items: list[AttentionItem] = []
        items.extend(await self._delivery_jobs(account_id=account_id, limit=limit))
        items.extend(await self._automation_runs(account_id=account_id, limit=limit))
        items.extend(await self._account_risks(account_id=account_id, limit=limit))
        items.sort(key=lambda item: item.since or datetime.min.replace(tzinfo=UTC), reverse=True)
        return items[:limit]

    async def list_message_notifications(
        self, *, since_minutes: int = 15, limit: int = 100
    ) -> list[AttentionItem]:
        """返回近期入站消息的通知事项。

        消息通知只由已配置且订阅 ``message_received`` 的通知渠道消费;它不会进入
        环境变量兜底 webhook 的人工待办队列。启用的 ``skip_notify`` 规则在这里生效,
        让过滤规则与 Helper 的消息通知语义真正连通。
        """
        if not 1 <= since_minutes <= 1440:
            raise ValueError("since_minutes 必须在 1 到 1440 之间")
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)
        statement = (
            select(Message, Account.account_id)
            .join(Account, Account.id == Message.account_id)
            .where(
                Message.direction == "inbound",
                Message.received_at >= cutoff,
            )
            .order_by(Message.received_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rules_statement = select(
            MessageFilterRule.account_id, MessageFilterRule.keyword
        ).where(
            MessageFilterRule.enabled.is_(True),
            MessageFilterRule.filter_type == "skip_notify",
        )
        async with get_async_session() as session:
            messages = (await session.execute(statement)).all()
            rules = (await session.execute(rules_statement)).all()
        blocked: dict[int, list[str]] = {}
        for account_pk, keyword in rules:
            blocked.setdefault(int(account_pk), []).append(str(keyword).casefold())
        items: list[AttentionItem] = []
        for message, owner in messages:
            content = message.content or ""
            if any(
                keyword and keyword in content.casefold()
                for keyword in blocked.get(int(message.account_id), [])
            ):
                continue
            message_id = int(message.id)
            items.append(
                AttentionItem(
                    kind=KIND_MESSAGE_RECEIVED,
                    target=f"message:{message_id}",
                    state="received",
                    reason=content[:500] or f"收到 {message.content_type} 消息",
                    next_command=f"xianyu-agent message show {message_id}",
                    account_id=owner,
                    since=message.received_at,
                )
            )
        return items

    async def _delivery_jobs(
        self, *, account_id: str | None, limit: int
    ) -> list[AttentionItem]:
        async with get_async_session() as session:
            jobs = await SqlAlchemyDeliveryJobRepository(session).list_jobs(
                account_id=account_id, states=HUMAN_JOB_STATES, limit=limit
            )
        return [
            AttentionItem(
                kind=KIND_DELIVERY_JOB,
                target=f"{job.account_id}/{job.order_id}",
                account_id=job.account_id,
                state=str(job.state),
                reason=job.last_error or "发货任务停在需要人工确认的状态",
                next_command=(
                    f"xianyu-agent delivery job show {job.local_id}; "
                    f"xianyu-agent delivery job resolve {job.local_id} "
                    "--delivered --note '<核对依据>' --yes"
                    if job.local_id is not None
                    else "xianyu-agent delivery job list"
                ),
                since=job.completed_at or job.started_at or job.created_at,
            )
            for job in jobs
        ]

    async def _automation_runs(
        self, *, account_id: str | None, limit: int
    ) -> list[AttentionItem]:
        statement = (
            select(PlatformAutomationRun, Account.account_id)
            .join(Account, Account.id == PlatformAutomationRun.account_id)
            .where(PlatformAutomationRun.status == "uncertain")
            .order_by(PlatformAutomationRun.id.desc())
            .limit(limit)
        )
        if account_id is not None:
            statement = statement.where(Account.account_id == account_id)
        async with get_async_session() as session:
            rows = (await session.execute(statement)).all()
        return [
            AttentionItem(
                kind=KIND_AUTOMATION_RUN,
                target=f"#{run.id} {run.action} {run.target_id}",
                account_id=owner,
                state=run.status,
                reason=run.result or "平台动作结果不明",
                next_command=(
                    f"xianyu-agent automation resolve --run-id {run.id} "
                    "--status completed|failed --note '<核对依据>'"
                ),
                since=run.finished_at or run.started_at,
            )
            for run, owner in rows
        ]

    async def _account_risks(
        self, *, account_id: str | None, limit: int
    ) -> list[AttentionItem]:
        statement = (
            select(WorkerStatusRow, Account.account_id)
            .join(Account, Account.id == WorkerStatusRow.account_id)
            .where(
                (WorkerStatusRow.risk_recovery_required.is_(True))
                | (WorkerStatusRow.status == WorkerState.NEEDS_VALIDATION.value)
            )
            .order_by(WorkerStatusRow.updated_at.desc())
            .limit(limit)
        )
        if account_id is not None:
            statement = statement.where(Account.account_id == account_id)
        async with get_async_session() as session:
            rows = (await session.execute(statement)).all()
        items: list[AttentionItem] = []
        for status_row, owner in rows:
            risk_detail = status_row.risk_code or status_row.last_error or "账号需要人工验证"
            items.append(
                AttentionItem(
                    kind=KIND_ACCOUNT_RISK,
                    target=owner,
                    account_id=owner,
                    state=status_row.status,
                    reason=risk_detail,
                    next_command=(
                        f"xianyu-agent auth status -a {owner} "
                        f"&& xianyu-agent auth refresh -a {owner}"
                    ),
                    since=status_row.risk_detected_at or status_row.updated_at,
                )
            )
        return items
