"""Regression contracts for the protocol diagnostics capture migration."""

from __future__ import annotations

import json

from xianyu_agent.protocol import capture as legacy_capture
from xianyu_agent.protocol.diagnostics import capture as canonical_capture


def test_legacy_capture_reexports_canonical_contract() -> None:
    names = (
        "CalibrationRecorder",
        "CaptureCounts",
        "CaptureVerification",
        "redact_structure",
        "verify_capture",
    )
    for name in names:
        assert getattr(legacy_capture, name) is getattr(canonical_capture, name)


def test_canonical_redaction_matches_legacy_and_never_leaks_secrets() -> None:
    salt = b"diagnostics-capture-migration-salt"
    payload = {
        "Cookie": "cookie-secret-value",
        "token": "token-secret-value",
        "authorization": "Bearer access-secret-value",
        "reminderUrl": (
            "https://example.test/a/b?token=query-secret-value&itemId=item-secret-value"
        ),
        "bizTag": json.dumps(
            {"messageId": "message-secret-value", "content": "buyer-secret-value"}
        ),
    }

    canonical = canonical_capture.redact_structure(payload, salt=salt)
    legacy = legacy_capture.redact_structure(payload, salt=salt)

    assert canonical == legacy
    rendered = json.dumps(canonical, ensure_ascii=False)
    for secret in (
        "cookie-secret-value",
        "token-secret-value",
        "access-secret-value",
        "query-secret-value",
        "item-secret-value",
        "message-secret-value",
        "buyer-secret-value",
    ):
        assert secret not in rendered
    assert canonical["reminderUrl"]["query_keys"] == ["itemId", "token"]
    assert canonical["bizTag"]["_encoding"] == "json"
