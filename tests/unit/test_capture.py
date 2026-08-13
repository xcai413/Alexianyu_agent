"""Protocol capture redaction and account connection lock tests."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from xianyu_agent.protocol.capture import CalibrationRecorder, redact_structure, verify_capture
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.events import MessageReceived, WsFrame
from xianyu_agent.services.account_lock import (
    AccountConnectionAlreadyRunningError,
    AccountConnectionLock,
)


def _sync_frame(secret: str) -> WsFrame:
    decoded = {
        "1": {
            "2": "chat-secret@goofish",
            "5": 1700000000000,
            "10": {
                "senderUserId": "buyer-secret",
                "reminderContent": secret,
                "bizTag": '{"messageId":"message-secret","itemId":"item-secret"}',
            },
        }
    }
    encoded = base64.b64encode(json.dumps(decoded).encode()).decode()
    return WsFrame(
        headers={"mid": "mid-secret", "sid": "sid-secret"},
        body={"syncPushPackage": {"data": [{"data": encoded}]}},
    )


@pytest.mark.asyncio
async def test_calibration_recorder_never_writes_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    recorder = CalibrationRecorder(path, account_id="account-secret")
    frame = _sync_frame("buyer-content-secret")
    event = MessageReceived(
        event_id="event-secret",
        account_id="account-secret",
        received_at=datetime.now(UTC),
        chat_id="chat-secret",
        message_id="message-secret",
        item_id="item-secret",
        sender_id="buyer-secret",
        sender_name="nickname-secret",
        content="buyer-content-secret",
        raw={"token": "token-secret", "Cookie": "cookie-secret"},
    )
    await recorder.record_frame(frame)
    await recorder.record_event(event)
    recorder.record_summary({"messages": 1, "missing_message_fields": []})

    content = path.read_text(encoding="utf-8")
    for secret in (
        "account-secret",
        "buyer-content-secret",
        "buyer-secret",
        "chat-secret",
        "message-secret",
        "item-secret",
        "nickname-secret",
        "mid-secret",
        "sid-secret",
        "token-secret",
        "cookie-secret",
    ):
        assert secret not in content
    records = [json.loads(line) for line in content.splitlines()]
    assert [record["kind"] for record in records] == ["frame", "event", "summary"]
    assert records[0]["decoded_sync"] is not None
    assert recorder.counts.frames == 1
    assert recorder.counts.events == 1


@pytest.mark.asyncio
async def test_capture_salt_changes_digest_between_runs(tmp_path: Path) -> None:
    first = CalibrationRecorder(tmp_path / "first.jsonl", account_id="same-account")
    second = CalibrationRecorder(tmp_path / "second.jsonl", account_id="same-account")
    event = MessageReceived(
        event_id="same-event",
        account_id="same-account",
        received_at=datetime.now(UTC),
        chat_id="same-chat",
        sender_id="same-buyer",
        content="same-content",
    )
    await first.record_event(event)
    await second.record_event(event)
    assert first.path.read_text(encoding="utf-8") != second.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_verify_capture_passes_complete_redacted_artifact(tmp_path: Path) -> None:
    recorder = CalibrationRecorder(tmp_path / "complete.jsonl", account_id="account")
    event = MessageReceived(
        event_id="event",
        account_id="account",
        received_at=datetime.now(UTC),
        chat_id="chat",
        message_id="message",
        item_id="item",
        sent_at=datetime.now(UTC),
        sender_id="buyer",
        content="content",
    )
    await recorder.record_frame(_sync_frame("content"))
    await recorder.record_event(event)
    recorder.record_summary(
        {
            "connected": True,
            "messages": 1,
            "target_reached": True,
            "missing_message_fields": [],
            "duplicate_message_events": 0,
            "errors": 0,
        }
    )
    result = verify_capture(recorder.path)
    assert result.ok is True
    assert result.frames == 1
    assert result.events == 1
    assert result.messages == 1


def test_verify_capture_rejects_plaintext_and_incomplete_summary(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps(
            {
                "kind": "summary",
                "payload": {"content": "plaintext buyer message"},
                "summary": {
                    "connected": True,
                    "messages": 0,
                    "target_reached": False,
                    "missing_message_fields": ["item_id"],
                    "duplicate_message_events": 1,
                    "errors": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    result = verify_capture(path)
    assert result.ok is False
    assert any("未达到" in issue for issue in result.issues)
    assert any("未脱敏" in issue for issue in result.issues)


def test_redact_structure_keeps_only_protocol_shape() -> None:
    redacted = redact_structure(
        {
            "code": 200,
            "timestamp": 1700000000000,
            "amount": 9.9,
            "type": "text",
            "bizTag": '{"messageId":"m-secret","itemId":"i-secret"}',
            "reminderUrl": "https://example.test/a/b?itemId=i-secret&foo=bar",
        }
    )
    assert redacted["code"] == 200
    assert redacted["timestamp"] == "<timestamp>"
    assert redacted["amount"] == "<number>"
    assert redacted["type"] == "text"
    assert redacted["bizTag"]["_encoding"] == "json"
    assert set(redacted["bizTag"]["value"]) == {"messageId", "itemId"}
    assert redacted["reminderUrl"]["_encoding"] == "url"
    assert redacted["reminderUrl"]["query_keys"] == ["foo", "itemId"]


@pytest.mark.asyncio
async def test_second_client_fails_account_lock_without_network(tmp_path: Path) -> None:
    path = tmp_path / "account.lock"
    first_lock = AccountConnectionLock(path)
    first_lock.acquire(owner_id="first")
    client = WsClient(
        "same-account",
        config=ClientConfig(ws_url="ws://unused"),
        account_lock=AccountConnectionLock(path),
    )
    try:
        with pytest.raises(AccountConnectionAlreadyRunningError):
            client.start()
    finally:
        first_lock.release()
