"""SQLAlchemy unit-of-work adapter."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xianyu_agent.db import database as legacy_database
from xianyu_agent.infrastructure.database.repositories.account import SqlAlchemyAccountRepository
from xianyu_agent.infrastructure.database.repositories.outbox import SqlAlchemyOutboxRepository


class SqlAlchemyUnitOfWork:
    """Own one ``AsyncSession`` and expose repository adapters for a use case."""

    session: AsyncSession
    accounts: SqlAlchemyAccountRepository
    outbox: SqlAlchemyOutboxRepository

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        session_factory = self._session_factory or legacy_database.async_session_factory
        self.session = session_factory()
        self.accounts = SqlAlchemyAccountRepository(self.session)
        self.outbox = SqlAlchemyOutboxRepository(self.session)
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            await self.rollback()
        finally:
            await self.session.close()

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
