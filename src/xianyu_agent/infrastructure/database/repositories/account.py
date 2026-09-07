"""SQLAlchemy implementation of the account repository port."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.application.ports.repositories import AccountRecord
from xianyu_agent.db.models import Account, WorkerDesiredState


class SqlAlchemyAccountRepository:
    """Account repository backed by one caller-owned ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_account_id(self, account_id: str) -> AccountRecord | None:
        row = await self._get_model(account_id)
        return None if row is None else self._to_record(row)

    async def list(
        self,
        *,
        only_enabled: bool = False,
        desired_state: str | None = None,
    ) -> Sequence[AccountRecord]:
        stmt = select(Account).order_by(Account.created_at.desc(), Account.id.desc())
        if only_enabled:
            stmt = stmt.where(Account.enabled.is_(True))
        if desired_state is not None:
            self._validate_desired_state(desired_state)
            stmt = stmt.where(Account.desired_state == desired_state)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_record(row) for row in rows]

    async def add(
        self,
        account_id: str,
        *,
        nickname: str | None = None,
        remark: str | None = None,
        enabled: bool = True,
    ) -> AccountRecord:
        existing = await self._get_model(account_id)
        if existing is not None:
            return self._to_record(existing)

        row = Account(
            account_id=account_id,
            nickname=nickname,
            remark=remark,
            enabled=enabled,
            desired_state=WorkerDesiredState.STOPPED.value,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return self._to_record(row)

    async def set_enabled(self, account_id: str, enabled: bool) -> bool:
        row = await self._get_model(account_id)
        if row is None:
            return False
        row.enabled = enabled
        if not enabled:
            row.desired_state = WorkerDesiredState.STOPPED.value
        await self._session.flush()
        return True

    async def set_desired_state(self, account_id: str, desired_state: str) -> bool:
        self._validate_desired_state(desired_state)
        row = await self._get_model(account_id)
        if row is None:
            return False
        row.desired_state = desired_state
        await self._session.flush()
        return True

    async def delete(self, account_id: str) -> bool:
        row = await self._get_model(account_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def _get_model(self, account_id: str) -> Account | None:
        stmt = select(Account).where(Account.account_id == account_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _validate_desired_state(desired_state: str) -> None:
        allowed = {WorkerDesiredState.RUNNING.value, WorkerDesiredState.STOPPED.value}
        if desired_state not in allowed:
            msg = f"invalid desired_state: {desired_state}"
            raise ValueError(msg)

    @staticmethod
    def _to_record(row: Account) -> AccountRecord:
        return AccountRecord(
            id=row.id,
            account_id=row.account_id,
            nickname=row.nickname,
            remark=row.remark,
            enabled=row.enabled,
            desired_state=row.desired_state,
            status=row.status,
            last_login_at=row.last_login_at,
            last_heartbeat_at=row.last_heartbeat_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
