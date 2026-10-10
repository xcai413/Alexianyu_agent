from __future__ import annotations

import pytest

from xianyu_agent.interfaces.web.auth.security import (
    AuthenticationError,
    hash_password,
    issue_session,
    verify_csrf,
    verify_password,
    verify_session,
)


def test_password_hash_never_contains_password_and_verifies() -> None:
    encoded = hash_password("correct horse battery staple")
    assert "correct horse" not in encoded
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_short_password_is_rejected() -> None:
    with pytest.raises(ValueError, match="12"):
        hash_password("short")


def test_session_requires_signature_and_csrf() -> None:
    token, csrf = issue_session(signing_key="test-signing-key", ttl_seconds=60)
    session = verify_session(token, signing_key="test-signing-key")
    verify_csrf(session, csrf)
    with pytest.raises(AuthenticationError):
        verify_session(f"{token}x", signing_key="test-signing-key")
    with pytest.raises(AuthenticationError):
        verify_csrf(session, "not-the-token")
