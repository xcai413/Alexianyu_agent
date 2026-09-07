"""Encrypted SecretVault contract shared by all supported databases."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from xianyu_agent.application.ports.secret_vault import SecretMetadata, SecretValue
from xianyu_agent.config import get_settings
from xianyu_agent.db import database as legacy_database
from xianyu_agent.infrastructure.database.models.secret import SecretRecord
from xianyu_agent.infrastructure.secrets import (
    FernetKeyring,
    SecretDecryptionError,
    SqlAlchemySecretVault,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_secret_vault_contract_across_backend() -> None:
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_vault = SqlAlchemySecretVault(FernetKeyring({"v1": old_key}, active_version="v1"))
    plaintext = f"secret-{backend}-{uuid4().hex}"
    ref = await old_vault.put(
        SecretValue(plaintext),
        metadata=SecretMetadata({"backend": backend}),
    )
    decoy_ref = await old_vault.put(SecretValue(f"decoy-{backend}-{uuid4().hex}"))
    assert await old_vault.configured(ref) is True
    assert (await old_vault.resolve(ref)).reveal() == plaintext

    async with legacy_database.async_session_factory() as session:
        record = await session.scalar(select(SecretRecord).where(SecretRecord.ref == ref.value))
        decoy = await session.scalar(
            select(SecretRecord).where(SecretRecord.ref == decoy_ref.value)
        )
        assert record is not None
        assert decoy is not None
        assert plaintext not in record.ciphertext
        assert record.metadata_json == {"backend": backend}
        original_ciphertext = record.ciphertext
        decoy_ciphertext = decoy.ciphertext

    async with legacy_database.async_session_factory() as session, session.begin():
        await session.execute(
            update(SecretRecord)
            .where(SecretRecord.ref == ref.value)
            .values(ciphertext=decoy_ciphertext)
        )
    with pytest.raises(SecretDecryptionError, match="reference binding mismatch"):
        await old_vault.resolve(ref)

    async with legacy_database.async_session_factory() as session, session.begin():
        await session.execute(
            update(SecretRecord)
            .where(SecretRecord.ref == ref.value)
            .values(ciphertext=original_ciphertext)
        )

    restored_vault = SqlAlchemySecretVault(
        FernetKeyring({"v1": old_key, "v2": new_key}, active_version="v2")
    )
    assert (await restored_vault.resolve(ref)).reveal() == plaintext
    assert await restored_vault.rotate(ref) is True
    assert await restored_vault.rotate(ref) is False

    active_only = SqlAlchemySecretVault(FernetKeyring({"v2": new_key}, active_version="v2"))
    assert (await active_only.resolve(ref)).reveal() == plaintext

    async with legacy_database.async_session_factory() as session, session.begin():
        await session.execute(
            update(SecretRecord)
            .where(SecretRecord.ref == ref.value)
            .values(ciphertext="tampered-ciphertext")
        )
    with pytest.raises(SecretDecryptionError, match="secret decryption failed"):
        await active_only.resolve(ref)

    await active_only.delete(ref)
    await old_vault.delete(decoy_ref)
    assert await active_only.configured(ref) is False
