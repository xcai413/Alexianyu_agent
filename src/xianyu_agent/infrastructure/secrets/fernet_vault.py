"""Fernet-backed durable SecretVault implementation."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xianyu_agent.application.ports.secret_vault import (
    SecretMetadata,
    SecretRef,
    SecretValue,
)
from xianyu_agent.db import database as legacy_database
from xianyu_agent.infrastructure.database.models.secret import SecretRecord

_MASTER_KEY_ENV = "XIANYU_SECRET_MASTER_KEY"
_MASTER_KEY_VERSION_ENV = "XIANYU_SECRET_MASTER_KEY_VERSION"
_PREVIOUS_KEYS_ENV = "XIANYU_SECRET_PREVIOUS_KEYS"


class MissingMasterKeyError(RuntimeError):
    """Raised when no master key is configured for the vault."""


class InvalidMasterKeyError(RuntimeError):
    """Raised when configured key material cannot initialize Fernet."""


class UnknownMasterKeyVersionError(RuntimeError):
    """Raised when ciphertext references a key version absent from the keyring."""


class SecretDecryptionError(RuntimeError):
    """Raised when ciphertext cannot be authenticated or decrypted."""


class SecretNotFoundError(KeyError):
    """Raised when an opaque secret reference no longer exists."""


class FernetKeyring:
    """Versioned Fernet keyring with one active key and optional legacy keys."""

    def __init__(self, keys: Mapping[str, str | bytes], *, active_version: str) -> None:
        if not active_version or not active_version.strip():
            raise InvalidMasterKeyError("active master-key version must not be empty")
        if active_version not in keys:
            raise InvalidMasterKeyError("active master-key version is not configured")

        fernets: dict[str, Fernet] = {}
        for version, key in keys.items():
            if not version or not version.strip():
                raise InvalidMasterKeyError("master-key version must not be empty")
            try:
                fernets[version] = Fernet(key.encode() if isinstance(key, str) else key)
            except (TypeError, ValueError) as exc:
                raise InvalidMasterKeyError("invalid Fernet master key") from exc

        self._fernets = fernets
        self.active_version = active_version

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> FernetKeyring:
        """Load active/legacy master keys without logging or persisting key material."""
        source = os.environ if environ is None else environ
        active_key = source.get(_MASTER_KEY_ENV)
        if not active_key:
            raise MissingMasterKeyError(f"{_MASTER_KEY_ENV} is required")

        active_version = source.get(_MASTER_KEY_VERSION_ENV, "v1")
        keys: dict[str, str | bytes] = {active_version: active_key}
        serialized_previous = source.get(_PREVIOUS_KEYS_ENV)
        if serialized_previous:
            try:
                previous = json.loads(serialized_previous)
            except json.JSONDecodeError as exc:
                raise InvalidMasterKeyError("legacy master-key map must be valid JSON") from exc
            if not isinstance(previous, dict) or not all(
                isinstance(version, str) and isinstance(key, str)
                for version, key in previous.items()
            ):
                raise InvalidMasterKeyError("legacy master-key map must be string-to-string JSON")
            keys.update(previous)
            keys[active_version] = active_key

        return cls(keys, active_version=active_version)

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        token = self._fernets[self.active_version].encrypt(plaintext.encode("utf-8"))
        return self.active_version, token.decode("ascii")

    def decrypt(self, key_version: str, ciphertext: str) -> str:
        fernet = self._fernets.get(key_version)
        if fernet is None:
            raise UnknownMasterKeyVersionError("secret key version is not available")
        try:
            plaintext = fernet.decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise SecretDecryptionError("secret decryption failed") from exc
        return plaintext.decode("utf-8")


class SqlAlchemySecretVault:
    """Durable SecretVault storing only authenticated ciphertext in SQLAlchemy."""

    def __init__(
        self,
        keyring: FernetKeyring,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._keyring = keyring
        self._session_factory = session_factory

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or legacy_database.async_session_factory

    async def put(
        self,
        secret: SecretValue,
        *,
        metadata: SecretMetadata | None = None,
    ) -> SecretRef:
        ref = SecretRef(f"sv1_{uuid4().hex}")
        key_version, ciphertext = self._keyring.encrypt(secret.reveal())
        async with self._sessions()() as session, session.begin():
            session.add(
                SecretRecord(
                    ref=ref.value,
                    key_version=key_version,
                    ciphertext=ciphertext,
                    metadata_json=dict(metadata.values) if metadata is not None else {},
                )
            )
        return ref

    async def replace(self, ref: SecretRef, secret: SecretValue) -> SecretRef:
        key_version, ciphertext = self._keyring.encrypt(secret.reveal())
        async with self._sessions()() as session, session.begin():
            record = await session.get(SecretRecord, ref.value)
            if record is None:
                raise SecretNotFoundError(ref.value)
            record.key_version = key_version
            record.ciphertext = ciphertext
        return ref

    async def resolve(self, ref: SecretRef) -> SecretValue:
        async with self._sessions()() as session:
            record = await session.get(SecretRecord, ref.value)
            if record is None:
                raise SecretNotFoundError(ref.value)
            plaintext = self._keyring.decrypt(record.key_version, record.ciphertext)
        return SecretValue(plaintext)

    async def delete(self, ref: SecretRef) -> None:
        async with self._sessions()() as session, session.begin():
            await session.execute(delete(SecretRecord).where(SecretRecord.ref == ref.value))

    async def configured(self, ref: SecretRef) -> bool:
        async with self._sessions()() as session:
            value = await session.scalar(select(SecretRecord.ref).where(SecretRecord.ref == ref.value))
        return value is not None

    async def rotate(self, ref: SecretRef) -> bool:
        """Re-encrypt one legacy record under the currently active master key."""
        async with self._sessions()() as session, session.begin():
            record = await session.get(SecretRecord, ref.value)
            if record is None:
                raise SecretNotFoundError(ref.value)
            if record.key_version == self._keyring.active_version:
                return False
            plaintext = self._keyring.decrypt(record.key_version, record.ciphertext)
            key_version, ciphertext = self._keyring.encrypt(plaintext)
            record.key_version = key_version
            record.ciphertext = ciphertext
        return True

    async def rotate_all(self) -> int:
        """Migrate all legacy ciphertext to the active key version."""
        async with self._sessions()() as session:
            refs = (
                await session.scalars(
                    select(SecretRecord.ref).where(
                        SecretRecord.key_version != self._keyring.active_version
                    )
                )
            ).all()
        rotated = 0
        for value in refs:
            rotated += int(await self.rotate(SecretRef(value)))
        return rotated
