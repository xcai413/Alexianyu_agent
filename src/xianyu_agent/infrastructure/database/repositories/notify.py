"""通知渠道仓储:事务由调用方拥有。"""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.domain.notify import NotificationChannel
from xianyu_agent.infrastructure.database.models import (
    Account as AccountRow,
    NotificationBinding as NotificationBindingRow,
    NotificationChannelRow,
)

DEFAULT_LIST_LIMIT = 200


def _to_channel(row: NotificationChannelRow) -> NotificationChannel:
    return NotificationChannel(
        name=row.name,
        kind=row.kind,
        target=row.target,
        secret_ref=row.secret_ref,
        events=tuple(row.events or ()),
        enabled=bool(row.enabled),
        description=row.description,
        local_id=row.id,
    )


class SqlAlchemyNotificationChannelRepository:
    """通知渠道与账号绑定的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, channel: NotificationChannel) -> int:
        row = NotificationChannelRow(
            name=channel.name,
            kind=channel.kind,
            target=channel.target,
            secret_ref=channel.secret_ref,
            events=list(channel.events),
            enabled=channel.enabled,
            description=channel.description,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def get(self, channel_id: int) -> NotificationChannel | None:
        row = await self._session.get(NotificationChannelRow, channel_id)
        return None if row is None else _to_channel(row)

    async def get_by_name(self, name: str) -> NotificationChannel | None:
        row = await self._session.scalar(
            select(NotificationChannelRow).where(NotificationChannelRow.name == name).limit(1)
        )
        return None if row is None else _to_channel(row)

    async def list_channels(
        self, *, only_enabled: bool = False, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[NotificationChannel]:
        statement = select(NotificationChannelRow).order_by(NotificationChannelRow.id).limit(limit)
        if only_enabled:
            statement = statement.where(NotificationChannelRow.enabled.is_(True))
        return [_to_channel(row) for row in (await self._session.scalars(statement)).all()]

    async def apply(self, channel: NotificationChannel) -> None:
        if channel.local_id is None:
            msg = "更新通知渠道需要 local_id"
            raise ValueError(msg)
        result = await self._session.execute(
            update(NotificationChannelRow)
            .where(NotificationChannelRow.id == channel.local_id)
            .values(
                name=channel.name,
                kind=channel.kind,
                target=channel.target,
                secret_ref=channel.secret_ref,
                events=list(channel.events),
                enabled=channel.enabled,
                description=channel.description,
            )
        )
        if not result.rowcount:
            msg = f"通知渠道 #{channel.local_id} 不存在"
            raise ValueError(msg)

    async def delete(self, channel_id: int) -> None:
        await self._session.execute(
            delete(NotificationChannelRow).where(NotificationChannelRow.id == channel_id)
        )

    # ---- 账号绑定 --------------------------------------------------------

    async def bind(self, channel_id: int, account_id: str) -> bool:
        """绑定账号;账号不存在返回 False,不会隐式建账号。"""
        account_row_id = await self._session.scalar(
            select(AccountRow.id).where(AccountRow.account_id == account_id).limit(1)
        )
        if account_row_id is None:
            return False
        exists = await self._session.scalar(
            select(NotificationBindingRow.id)
            .where(
                NotificationBindingRow.channel_id == channel_id,
                NotificationBindingRow.account_id == int(account_row_id),
            )
            .limit(1)
        )
        if exists is not None:
            return True
        self._session.add(
            NotificationBindingRow(channel_id=channel_id, account_id=int(account_row_id))
        )
        await self._session.flush()
        return True

    async def unbind(self, channel_id: int, account_id: str) -> bool:
        account_row_id = await self._session.scalar(
            select(AccountRow.id).where(AccountRow.account_id == account_id).limit(1)
        )
        if account_row_id is None:
            return False
        result = await self._session.execute(
            delete(NotificationBindingRow).where(
                NotificationBindingRow.channel_id == channel_id,
                NotificationBindingRow.account_id == int(account_row_id),
            )
        )
        return bool(result.rowcount)

    async def bound_accounts(self, channel_id: int) -> list[str]:
        rows = await self._session.execute(
            select(AccountRow.account_id)
            .join(NotificationBindingRow, NotificationBindingRow.account_id == AccountRow.id)
            .where(NotificationBindingRow.channel_id == channel_id)
            .order_by(AccountRow.account_id)
        )
        return [str(value) for value in rows.scalars().all()]

    async def bindings_by_channel(self) -> dict[int, set[str]]:
        """一次取全部绑定,避免派发时为每条渠道各查一遍库。"""
        rows = await self._session.execute(
            select(NotificationBindingRow.channel_id, AccountRow.account_id).join(
                AccountRow, AccountRow.id == NotificationBindingRow.account_id
            )
        )
        grouped: dict[int, set[str]] = {}
        for channel_id, account_id in rows.all():
            grouped.setdefault(int(channel_id), set()).add(str(account_id))
        return grouped
