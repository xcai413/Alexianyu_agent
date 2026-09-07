"""Account use cases coordinated by the application layer."""

from __future__ import annotations

from collections.abc import Sequence

from .ports.repositories import AccountRecord
from .ports.unit_of_work import UnitOfWorkFactory

RUNNING_DESIRED_STATE = "running"


class AccountApplication:
    """Account CRUD and desired-state use cases over a unit-of-work boundary."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def create_account(
        self,
        account_id: str,
        *,
        nickname: str | None = None,
        remark: str | None = None,
        enabled: bool = True,
    ) -> AccountRecord:
        async with self._uow_factory() as uow:
            row = await uow.accounts.add(
                account_id,
                nickname=nickname,
                remark=remark,
                enabled=enabled,
            )
            await uow.commit()
            return row

    async def get_account(self, account_id: str) -> AccountRecord | None:
        async with self._uow_factory() as uow:
            return await uow.accounts.get_by_account_id(account_id)

    async def list_accounts(self, *, only_enabled: bool = False) -> Sequence[AccountRecord]:
        async with self._uow_factory() as uow:
            return await uow.accounts.list(only_enabled=only_enabled)

    async def list_desired_running_accounts(self) -> Sequence[AccountRecord]:
        async with self._uow_factory() as uow:
            return await uow.accounts.list(
                only_enabled=True,
                desired_state=RUNNING_DESIRED_STATE,
            )

    async def set_enabled(self, account_id: str, enabled: bool) -> bool:
        async with self._uow_factory() as uow:
            updated = await uow.accounts.set_enabled(account_id, enabled)
            if updated:
                await uow.commit()
            return updated

    async def set_desired_state(self, account_id: str, desired_state: str) -> bool:
        async with self._uow_factory() as uow:
            updated = await uow.accounts.set_desired_state(account_id, desired_state)
            if updated:
                await uow.commit()
            return updated

    async def set_remark(self, account_id: str, remark: str | None) -> bool:
        async with self._uow_factory() as uow:
            updated = await uow.accounts.set_remark(account_id, remark)
            if updated:
                await uow.commit()
            return updated

    async def delete_account(self, account_id: str) -> bool:
        async with self._uow_factory() as uow:
            deleted = await uow.accounts.delete(account_id)
            if deleted:
                await uow.commit()
            return deleted
