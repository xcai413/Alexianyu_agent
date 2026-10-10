"""Revoked administrator sessions; cookie contents are never persisted."""

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RevokedWebSession(Base):
    __tablename__ = "revoked_web_sessions"

    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
