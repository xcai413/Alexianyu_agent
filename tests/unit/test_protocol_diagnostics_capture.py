"""Regression contracts for the protocol diagnostics capture migration."""

from __future__ import annotations

import base64
import json

import msgpack

from xianyu_agent.protocol import capture as legacy_capture
from xianyu_agent.protocol.diagnostics import capture as canonical_capture
from xianyu_agent.protocol.events import WsFrame


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



def test_capture_reports_raw_msgpack_integer_key_count() -> None:
    raw = msgpack.packb(
        {
            1: {
                10: {
                    "senderUserId": "buyer",
                }
            }
        },
        use_bin_type=True,
    )
    frame = WsFrame(
        body={
            "syncPushPackage": {
                "data": [
                    {
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                ]
            }
        }
    )

    assert canonical_capture.raw_sync_key_types(frame) == {
        "integer_keys": 2,
        "string_keys": 1,
        "other_keys": 0,
    }


async def test_calibration_recorder_persists_raw_sync_key_types(tmp_path):
    raw = msgpack.packb(
        {
            1: {
                10: {
                    "senderUserId": "buyer",
                }
            }
        },
        use_bin_type=True,
    )
    frame = WsFrame(
        body={
            "syncPushPackage": {
                "data": [
                    {
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                ]
            }
        }
    )
    path = tmp_path / "capture.jsonl"
    recorder = canonical_capture.CalibrationRecorder(path, account_id="xcaicai")

    await recorder.record_frame(frame)

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["raw_sync_key_types"] == {
        "integer_keys": 2,
        "string_keys": 1,
        "other_keys": 0,
    }
