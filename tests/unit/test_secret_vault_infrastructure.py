"""Unit contracts for SecretVault master-key handling."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from xianyu_agent.infrastructure.secrets import (
    FernetKeyring,
    InvalidMasterKeyError,
    MissingMasterKeyError,
    SecretDecryptionError,
    UnknownMasterKeyVersionError,
)


def test_environment_requires_master_key() -> None:
    with pytest.raises(MissingMasterKeyError, match="XIANYU_SECRET_MASTER_KEY is required"):
        FernetKeyring.from_environment({})


def test_environment_loads_active_and_legacy_keys_for_rotation() -> None:
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    keyring = FernetKeyring.from_environment(
        {
            "XIANYU_SECRET_MASTER_KEY": new_key,
            "XIANYU_SECRET_MASTER_KEY_VERSION": "v2",
            "XIANYU_SECRET_PREVIOUS_KEYS": json.dumps({"v1": old_key}),
        }
    )
    old_ring = FernetKeyring({"v1": old_key}, active_version="v1")
    version, ciphertext = old_ring.encrypt("backup-compatible")

    assert keyring.decrypt(version, ciphertext) == "backup-compatible"
    assert keyring.active_version == "v2"


def test_invalid_or_unknown_master_key_fails_closed() -> None:
    with pytest.raises(InvalidMasterKeyError, match="invalid Fernet master key"):
        FernetKeyring({"v1": "not-a-fernet-key"}, active_version="v1")

    keyring = FernetKeyring({"v2": Fernet.generate_key()}, active_version="v2")
    with pytest.raises(UnknownMasterKeyVersionError, match="key version is not available"):
        keyring.decrypt("v1", "ciphertext")


def test_wrong_key_and_tampered_ciphertext_fail_closed() -> None:
    first = FernetKeyring({"v1": Fernet.generate_key()}, active_version="v1")
    second = FernetKeyring({"v1": Fernet.generate_key()}, active_version="v1")
    version, ciphertext = first.encrypt("do-not-leak")

    with pytest.raises(SecretDecryptionError, match="secret decryption failed"):
        second.decrypt(version, ciphertext)
    with pytest.raises(SecretDecryptionError, match="secret decryption failed"):
        first.decrypt(version, ciphertext[:-2] + "xx")
