"""擦亮与求花自动化的校准门禁、配置和幂等事实。

本模块不调用闲鱼接口。协议动作只有在真实校准被人工登记后才允许启用;实际调度器后续只消费
这里的启用配置,并必须先占用幂等键。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from xianyu_agent.db import (
    Account,
    PlatformAutomationConfig,
    PlatformAutomationRun,
    get_async_session,
)

ACTION_POLISH = "polish"
ACTION_REQUEST_FLOWER = "request_flower"
PLATFORM_ACTIONS = (ACTION_POLISH, ACTION_REQUEST_FLOWER)
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_QUIET_HOURS = re.compile(
    r"^(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d$"
)


@dataclass(frozen=True, slots=True)
class PlatformAutomationView:
    account_id: str
    action: str
    enabled: bool
    calibrated: bool
    calibration_approved_at: datetime | None
    calibration_approved_by: str | None
    schedule_time: str
    delay_minutes: int
    quiet_hours: str | None


@dataclass(frozen=True, slots=True)
class PlatformAutomationRunView:
    id: int
    account_id: str
    action: str
    target_id: str
    status: str
    result: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class SellerFeaturesView:
    """对照 super-butler seller-features 的只读投影,不另存一份开关。"""

    account_id: str
    polish_enabled: bool
    request_flower_enabled: bool
    auto_rate_enabled: bool
    auto_thanks_enabled: bool
    unsupported: tuple[str, ...]


class PlatformAutomationService:
    """Persist automation intent while failing closed before calibration."""

    async def seller_features(self, account_id: str) -> SellerFeaturesView:
        """读取卖家互动能力投影;评价/感谢不会被伪装成已支持。"""
        rows = await self.list(account_id)
        by_action = {row.action: row for row in rows}
        return SellerFeaturesView(
            account_id=account_id,
            polish_enabled=bool(by_action.get(ACTION_POLISH) and by_action[ACTION_POLISH].enabled),
            request_flower_enabled=bool(
                by_action.get(ACTION_REQUEST_FLOWER)
                and by_action[ACTION_REQUEST_FLOWER].enabled
            ),
            auto_rate_enabled=False,
            auto_thanks_enabled=False,
            unsupported=("auto_rate", "auto_thanks"),
        )

    async def list(self, account_id: str) -> list[PlatformAutomationView]:
        async with get_async_session() as session:
            account = await self._account(session, account_id)
            rows = list(
                (
                    await session.scalars(
                        select(PlatformAutomationConfig)
                        .where(PlatformAutomationConfig.account_id == account.id)
                        .order_by(PlatformAutomationConfig.action)
                    )
                ).all()
            )
        return [self._view(account_id, row) for row in rows]

    async def get(
        self, account_id: str, action: str
    ) -> PlatformAutomationView | None:
        normalized = self._action(action)
        async with get_async_session() as session:
            account = await self._account(session, account_id)
            row = await self._config(session, account.id, normalized)
        return None if row is None else self._view(account_id, row)

    async def list_executable(self) -> list[PlatformAutomationView]:
        """Return only enabled, calibrated actions for enabled accounts."""
        async with get_async_session() as session:
            rows = (
                await session.execute(
                    select(Account.account_id, PlatformAutomationConfig)
                    .join(
                        PlatformAutomationConfig,
                        PlatformAutomationConfig.account_id == Account.id,
                    )
                    .where(
                        Account.enabled.is_(True),
                        PlatformAutomationConfig.enabled.is_(True),
                        PlatformAutomationConfig.calibration_approved_at.is_not(None),
                    )
                    .order_by(Account.account_id, PlatformAutomationConfig.action)
                )
            ).all()
        return [self._view(account_id, row) for account_id, row in rows]

    async def is_executable(self, account_id: str, action: str) -> bool:
        normalized = self._action(action)
        async with get_async_session() as session:
            row = await session.scalar(
                select(PlatformAutomationConfig.id)
                .join(Account, Account.id == PlatformAutomationConfig.account_id)
                .where(
                    Account.account_id == account_id.strip(),
                    Account.enabled.is_(True),
                    PlatformAutomationConfig.action == normalized,
                    PlatformAutomationConfig.enabled.is_(True),
                    PlatformAutomationConfig.calibration_approved_at.is_not(None),
                )
                .limit(1)
            )
        return row is not None

    async def list_runs(
        self, account_id: str, *, limit: int = 50
    ) -> list[PlatformAutomationRunView]:
        if not 1 <= limit <= 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        async with get_async_session() as session:
            account = await self._account(session, account_id)
            rows = list(
                (
                    await session.scalars(
                        select(PlatformAutomationRun)
                        .where(PlatformAutomationRun.account_id == account.id)
                        .order_by(
                            PlatformAutomationRun.started_at.desc(),
                            PlatformAutomationRun.id.desc(),
                        )
                        .limit(limit)
                    )
                ).all()
            )
        return [self._run_view(account_id, row) for row in rows]

    async def approve_calibration(
        self, account_id: str, action: str, *, approved_by: str
    ) -> PlatformAutomationView:
        normalized = self._action(action)
        actor = approved_by.strip()
        if not actor:
            raise ValueError("校准批准人不能为空")
        async with get_async_session() as session, session.begin():
            account = await self._account(session, account_id)
            row = await self._config(session, account.id, normalized)
            if row is None:
                row = PlatformAutomationConfig(
                    account_id=account.id,
                    action=normalized,
                    enabled=False,
                    schedule_time=self._default_time(normalized),
                    delay_minutes=self._default_delay(normalized),
                    quiet_hours=self._default_quiet_hours(normalized),
                )
                session.add(row)
            row.calibration_approved_at = datetime.now(UTC)
            row.calibration_approved_by = actor
            await session.flush()
            view = self._view(account_id, row)
        return view

    async def revoke_calibration(
        self, account_id: str, action: str
    ) -> PlatformAutomationView:
        normalized = self._action(action)
        async with get_async_session() as session, session.begin():
            account = await self._account(session, account_id)
            row = await self._config(session, account.id, normalized)
            if row is None:
                raise ValueError(f"账号 {account_id} 尚无 {normalized} 自动化配置")
            row.enabled = False
            row.calibration_approved_at = None
            row.calibration_approved_by = None
            await session.flush()
            view = self._view(account_id, row)
        return view

    async def configure(
        self,
        account_id: str,
        action: str,
        *,
        enabled: bool,
        schedule_time: str | None = None,
        delay_minutes: int | None = None,
        quiet_hours: str | None = None,
    ) -> PlatformAutomationView:
        normalized = self._action(action)
        resolved_time = schedule_time or self._default_time(normalized)
        resolved_delay = (
            self._default_delay(normalized) if delay_minutes is None else delay_minutes
        )
        resolved_quiet_hours = (
            quiet_hours
            if quiet_hours is not None
            else self._default_quiet_hours(normalized)
        )
        self._validate_schedule(resolved_time, resolved_delay, resolved_quiet_hours)
        async with get_async_session() as session, session.begin():
            account = await self._account(session, account_id)
            row = await self._config(session, account.id, normalized)
            if enabled and (row is None or row.calibration_approved_at is None):
                raise ValueError(
                    f"{normalized} 尚未完成真实校准,先按待真实验证清单执行并登记批准"
                )
            if row is None:
                row = PlatformAutomationConfig(account_id=account.id, action=normalized)
                session.add(row)
            row.enabled = enabled
            row.schedule_time = resolved_time
            row.delay_minutes = resolved_delay
            row.quiet_hours = resolved_quiet_hours
            await session.flush()
            view = self._view(account_id, row)
        return view

    async def reserve_run(
        self,
        account_id: str,
        action: str,
        *,
        target_id: str,
        idempotency_key: str,
    ) -> int | None:
        """Reserve one external action. Duplicate keys return None and never run twice."""
        normalized = self._action(action)
        if not target_id.strip() or not idempotency_key.strip():
            raise ValueError("target_id 与 idempotency_key 不能为空")
        async with get_async_session() as session:
            account = await self._account(session, account_id)
            row = PlatformAutomationRun(
                account_id=account.id,
                action=normalized,
                target_id=target_id.strip(),
                idempotency_key=idempotency_key.strip(),
                status="running",
            )
            session.add(row)
            try:
                await session.flush()
                run_id = int(row.id)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            return run_id

    async def finish_run(self, run_id: int, *, status: str, result: str | None = None) -> None:
        if status not in {"completed", "failed", "uncertain", "skipped"}:
            raise ValueError(f"未知自动化执行状态: {status}")
        async with get_async_session() as session, session.begin():
            row = await session.get(PlatformAutomationRun, run_id)
            if row is None:
                raise ValueError(f"自动化执行 #{run_id} 不存在")
            row.status = status
            row.result = (result or "")[:255] or None
            row.finished_at = datetime.now(UTC)

    async def resolve_run(
        self,
        run_id: int,
        *,
        status: str,
        note: str,
        resolved_by: str,
    ) -> PlatformAutomationRunView:
        """人工收尾一条结果不明的执行事实。

        只有 ``uncertain`` 需要人工判断:自动流程既不重发也不退库存,由运营核对平台后把它
        定成完成或失败。已经是终态的记录不能被改写,避免把历史事实覆盖掉。
        """
        if status not in {"completed", "failed"}:
            msg = f"人工收尾只能标记为 completed 或 failed,收到 {status!r}"
            raise ValueError(msg)
        actor = resolved_by.strip()
        if not actor:
            msg = "收尾人不能为空"
            raise ValueError(msg)
        text = note.strip()
        if not text:
            msg = "人工收尾必须写明依据(例如平台订单页已确认发货)"
            raise ValueError(msg)
        async with get_async_session() as session, session.begin():
            row = await session.get(PlatformAutomationRun, run_id)
            if row is None:
                msg = f"自动化执行 #{run_id} 不存在"
                raise ValueError(msg)
            if row.status != "uncertain":
                msg = f"自动化执行 #{run_id} 当前状态是 {row.status},不是结果不明,无需人工收尾"
                raise ValueError(msg)
            row.status = status
            row.result = f"人工收尾({actor}): {text}"[:255]
            row.finished_at = datetime.now(UTC)
            await session.flush()
            account = await session.get(Account, row.account_id)
            account_id = account.account_id if account is not None else ""
            view = self._run_view(account_id, row)
        return view

    @staticmethod
    async def _account(session, account_id: str) -> Account:
        row = await session.scalar(
            select(Account).where(Account.account_id == account_id.strip()).limit(1)
        )
        if row is None:
            raise ValueError(f"账号 {account_id} 不存在")
        return row

    @staticmethod
    async def _config(session, account_pk: int, action: str):
        return await session.scalar(
            select(PlatformAutomationConfig)
            .where(
                PlatformAutomationConfig.account_id == account_pk,
                PlatformAutomationConfig.action == action,
            )
            .limit(1)
        )

    @staticmethod
    def _action(action: str) -> str:
        normalized = action.strip().lower()
        if normalized not in PLATFORM_ACTIONS:
            raise ValueError(f"未知平台自动化动作: {action},可选 {PLATFORM_ACTIONS}")
        return normalized

    @staticmethod
    def _default_time(action: str) -> str:
        return "03:00" if action == ACTION_POLISH else "10:00"

    @staticmethod
    def _default_delay(action: str) -> int:
        return 0 if action == ACTION_POLISH else 24 * 60

    @staticmethod
    def _default_quiet_hours(action: str) -> str | None:
        return "23:00-08:00" if action == ACTION_REQUEST_FLOWER else None

    @staticmethod
    def _validate_schedule(schedule_time: str, delay_minutes: int, quiet_hours: str | None) -> None:
        if not _CLOCK.fullmatch(schedule_time):
            raise ValueError("schedule_time 必须是 HH:MM")
        if not 0 <= delay_minutes <= 30 * 24 * 60:
            raise ValueError("delay_minutes 必须在 0 到 43200 之间")
        if quiet_hours is not None and not _QUIET_HOURS.fullmatch(quiet_hours):
            raise ValueError("quiet_hours 必须是 HH:MM-HH:MM")
        if quiet_hours is not None:
            start, end = quiet_hours.split("-", maxsplit=1)
            if start == end:
                raise ValueError("quiet_hours 起止时间不能相同")

    @staticmethod
    def _view(account_id: str, row: PlatformAutomationConfig) -> PlatformAutomationView:
        return PlatformAutomationView(
            account_id=account_id,
            action=row.action,
            enabled=bool(row.enabled),
            calibrated=row.calibration_approved_at is not None,
            calibration_approved_at=row.calibration_approved_at,
            calibration_approved_by=row.calibration_approved_by,
            schedule_time=row.schedule_time,
            delay_minutes=row.delay_minutes,
            quiet_hours=row.quiet_hours,
        )

    @staticmethod
    def _run_view(
        account_id: str, row: PlatformAutomationRun
    ) -> PlatformAutomationRunView:
        return PlatformAutomationRunView(
            id=int(row.id),
            account_id=account_id,
            action=row.action,
            target_id=row.target_id,
            status=row.status,
            result=row.result,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )
