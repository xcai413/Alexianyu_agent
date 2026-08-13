"""Local-WS integration helpers; never call Xianyu endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from xianyu_agent.db import Account, WsCredential, get_async_session
from xianyu_agent.protocol.signer import CookieSigner


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
