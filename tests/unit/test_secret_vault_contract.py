"""Contracts for the application-layer SecretVault port."""

from __future__ import annotations

import pytest

from xianyu_agent.application.ports.secret_vault import (
    SecretMetadata,
    SecretRef,
    SecretValue,
)


def test_secret_value_redacts_string_and_repr() -> None:
    secret = SecretValue("credential-must-never-leak")

    assert str(secret) == "<redacted>"
    assert repr(secret) == "SecretValue(<redacted>)"
    assert "credential-must-never-leak" not in str(secret)
    assert "credential-must-never-leak" not in repr(secret)
    assert secret.reveal() == "credential-must-never-leak"


def test_secret_ref_rejects_empty_reference() -> None:
    with pytest.raises(ValueError, match="secret reference must not be empty"):
        SecretRef("   ")


def test_secret_value_rejects_empty_plaintext() -> None:
    with pytest.raises(ValueError, match="secret value must not be empty"):
        SecretValue("")


def test_secret_metadata_is_defensively_frozen() -> None:
    source = {"provider": "example"}
    metadata = SecretMetadata(source)
    source["provider"] = "mutated"

    assert metadata.values == {"provider": "example"}
    with pytest.raises(TypeError):
        metadata.values["provider"] = "forbidden"  # type: ignore[index]
