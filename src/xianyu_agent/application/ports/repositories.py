"""Repository protocols consumed by the application layer."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """Persistence-neutral account projection used across repository boundaries."""

    id: int
    account_id: str
    nickname: str | None
    remark: str | None
    enabled: bool
    desired_state: str
    status: str
    last_login_at: datetime | None
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AccountRepository(Protocol):
    """Persistence contract for account configuration and desired runtime state.

    Repository methods never commit a transaction. The application/UoW boundary
    owns commit and rollback so multiple writes can later share one transaction.
    """

    async def get_by_account_id(self, account_id: str) -> AccountRecord | None: ...

    async def list(
        self,
        *,
        only_enabled: bool = False,
        desired_state: str | None = None,
    ) -> Sequence[AccountRecord]: ...

    async def add(
        self,
        account_id: str,
        *,
        nickname: str | None = None,
        remark: str | None = None,
        enabled: bool = True,
    ) -> AccountRecord: ...

    async def set_enabled(self, account_id: str, enabled: bool) -> bool: ...

    async def set_desired_state(self, account_id: str, desired_state: str) -> bool: ...

    async def delete(self, account_id: str) -> bool: ...
