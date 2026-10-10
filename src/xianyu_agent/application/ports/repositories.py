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
    # M2 账号归属租约;单机模式(没有实例身份)下始终为 None。放在末尾以保持构造兼容。
    owner_instance_id: str | None = None
    owner_lease_until: datetime | None = None
    takeover_approved_at: datetime | None = None
    takeover_approved_by: str | None = None
    takeover_failure_count: int = 0
    takeover_last_failure_at: datetime | None = None
    # 账号级自动回复暂停(到点自动恢复);放在末尾以保持构造兼容。
    reply_paused_until: datetime | None = None
    reply_paused_reason: str | None = None


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

    async def set_remark(self, account_id: str, remark: str | None) -> bool: ...

    async def set_nickname(self, account_id: str, nickname: str | None) -> bool: ...

    async def set_reply_pause(
        self,
        account_id: str,
        *,
        until: datetime | None,
        reason: str | None = None,
    ) -> bool: ...

    async def delete(self, account_id: str) -> bool: ...
