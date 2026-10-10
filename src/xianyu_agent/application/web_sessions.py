"""Durable revocation for signed administrator sessions across Web instances."""

from hashlib import sha256

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from xianyu_agent.db import get_async_session
from xianyu_agent.infrastructure.database.models import RevokedWebSession


class WebSessionService:
    @staticmethod
    async def is_revoked(token: str) -> bool:
        session_hash = sha256(token.encode()).hexdigest()
        async with get_async_session() as session:
            return await session.get(RevokedWebSession, session_hash) is not None

    @staticmethod
    async def revoke(token: str, *, expires_at: int) -> None:
        """Commit before confirming logout; tolerate a concurrent duplicate logout.

        Keep tombstones across expiry: another instance's slower clock or a clock
        rollback can otherwise make a deleted token valid again.
        """
        session_hash = sha256(token.encode()).hexdigest()
        async with get_async_session() as session:
            if session.get_bind().dialect.name == "sqlite":
                # Avoid a read/rollback/write lock cycle between copied logouts.
                # A single INSERT starts a write transaction and ignores only a
                # duplicate primary key, as in the existing idempotency adapter.
                await session.execute(
                    sqlite_insert(RevokedWebSession)
                    .values(session_hash=session_hash, expires_at=expires_at)
                    .on_conflict_do_nothing(index_elements=["session_hash"])
                )
                await session.commit()
                return
            session.add(RevokedWebSession(session_hash=session_hash, expires_at=expires_at))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.get(RevokedWebSession, session_hash)
                if existing is None or existing.expires_at != expires_at:
                    raise
