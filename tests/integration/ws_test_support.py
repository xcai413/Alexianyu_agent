"""Local-WS integration helpers; never call Xianyu endpoints."""

from __future__ import annotations

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
