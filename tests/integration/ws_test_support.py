"""Local-WS integration helpers; never call Xianyu endpoints."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from xianyu_agent.db import Account, WsCredential, get_async_session
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.protocol.ws.message_send import MESSAGE_SEND_LWP


async def cache_test_ws_token(
    account_id: str, signer: CookieSigner, *, token: str = "test-access-token"
) -> None:
    """Insert an encrypted, unexpired token so local WS tests stay offline."""
    async with get_async_session() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == account_id).limit(1)
            )
        ).scalar_one()
        session.add(
            WsCredential(
                account_id=account.id,
                encrypted_token=signer.fernet.encrypt(token.encode()).decode(),
                device_id=f"test-device-{account_id}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()


async def complete_test_registration(ws) -> list[str]:
    """Complete `/reg` confirmation and receive `ackDiff` for local WS tests."""
    registration_text = await ws.recv()
    registration = json.loads(registration_text)
    assert registration["lwp"] == "/reg"
    await ws.send(
        json.dumps(
            {
                "code": 200,
                "headers": {"mid": registration["headers"]["mid"], "sid": "test-session"},
            }
        )
    )
    ack_text = await ws.recv()
    ack = json.loads(ack_text)
    assert ack["code"] == 200
    assert ack["headers"]["mid"] == registration["headers"]["mid"]
    sync_text = await ws.recv()
    sync = json.loads(sync_text)
    assert sync["lwp"] == "/r/SyncStatus/ackDiff"
    return [registration_text, ack_text, sync_text]


async def acknowledge_text_message(ws, raw: str) -> bool:
    """ACK one calibrated outbound text request from the local mock server."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or payload.get("lwp") != MESSAGE_SEND_LWP:
        return False
    headers = payload.get("headers")
    if not isinstance(headers, dict) or not headers.get("mid"):
        return False
    await ws.send(
        json.dumps(
            {
                "code": 200,
                "headers": {"mid": headers["mid"]},
                "body": {"messageId": f"mock-{headers['mid']}"},
            }
        )
    )
    return True


def decode_outbound_text(raw: str) -> str | None:
    """Decode text content from one calibrated outbound message frame."""
    try:
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("lwp") != MESSAGE_SEND_LWP:
            return None
        body = payload["body"]
        encoded = body[0]["content"]["custom"]["data"]
        decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        text = decoded["text"]["text"]
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return text if isinstance(text, str) else None
