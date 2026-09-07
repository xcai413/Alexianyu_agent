"""Persistent encrypted SecretVault records."""

from __future__ import annotations

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SecretRecord(Base):
    """Encrypted secret payload keyed by an opaque application reference."""

    __tablename__ = "secret_vault_entries"

    ref: Mapped[str] = mapped_column(String(80), primary_key=True)
    key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, str]] = mapped_column("metadata", JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_secret_vault_entries_key_version", "key_version"),
    )
