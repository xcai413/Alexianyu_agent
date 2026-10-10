"""Persisted account gate for ordinary MTOP requests, not recovery probes."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from sqlalchemy import select

from xianyu_agent.db import Account, WorkerStatus, get_async_session
from xianyu_agent.domain import worker_risk
from xianyu_agent.protocol.mtop.signer import CookieSigner


class MtopRequestsPausedError(ValueError):
    """An account requires explicit, verified session recovery before requests."""


async def load_request_cookie(signer: CookieSigner, account_id: str) -> str | None:
    """Fail closed before loading credentials or opening an HTTP connection.

    The recovery flag, not the cooldown deadline, controls this gate. Expired
    cooldowns and process restarts never grant permission to ordinary requests.
    Local diagnostics and explicit credential recovery use the signer directly.
    """
    async with get_async_session() as session:
        required = await session.scalar(
            select(WorkerStatus.risk_recovery_required)
            .join(Account, Account.id == WorkerStatus.account_id)
            .where(Account.account_id == account_id)
        )
    if required:
        raise MtopRequestsPausedError("账号待人工验证与会话恢复,已暂停主动 MTOP 请求")
    return await signer.load_cookie_value(account_id)


async def observe_mtop_response(account_id: str, response: httpx.Response) -> None:
    """Persist explicit platform risk before parsers or bootstrap can continue."""
    try:
        body = response.json()
    except ValueError:
        return
    if not isinstance(body, Mapping):
        return
    ret = body.get("ret")
    entries = ret if isinstance(ret, list) else [ret]
    text = " ".join(entry for entry in entries if isinstance(entry, str))
    if not worker_risk.is_user_validate_error(text):
        return
    code = worker_risk.detected_risk_code(text)
    await worker_risk.open_user_validate(account_id, code=code)
    raise MtopRequestsPausedError(f"平台返回 {code},已暂停主动 MTOP 请求")
