"""待办事项的外部通知(对照 Helper 的 delivery_notifier)。

默认关闭:只有显式配置 `XIANYU_ALERT_WEBHOOK_URL` 才会发请求,而且必须过出站守卫。

三条纪律:

1. **不重复发**:同一事项(kind + target + state)只发一次,靠 `alert_dispatches` 的唯一键收敛,
   多副本同时扫描也只有一个会真正发出去。
2. **不阻塞主循环**:通知失败只记日志与发送事实,绝不抛给 daemon;通知是辅助,不是业务。
3. **不外泄敏感信息**:payload 只含事项的类型、目标、状态、原因与收尾命令,这些字段本身已经是不含
   密钥的运营信息;Cookie / Token / 卡密从不进入清单,因此也不会进入通知。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy.exc import IntegrityError

from xianyu_agent.db import AlertDispatch, get_async_session
from xianyu_agent.infrastructure.security.outbound_guard import check_url

from .service import AttentionItem

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AlertDispatchResult:
    """一次通知轮次的结果:发了多少、跳过多少、失败多少。"""

    sent: int = 0
    skipped: int = 0
    failed: int = 0
    disabled: bool = False


class AlertNotifier:
    """POST new attention items to one configured webhook, at most once each."""

    def __init__(
        self,
        *,
        url: str = "",
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_hosts: tuple[str, ...] = (),
        resolve_dns: bool = True,
    ) -> None:
        self._url = url.strip()
        self._timeout = timeout_seconds
        self._transport = transport
        self._allow_hosts = allow_hosts
        self._resolve_dns = resolve_dns

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def dispatch(self, items: Sequence[AttentionItem]) -> AlertDispatchResult:
        if not self._url or not items:
            return AlertDispatchResult(disabled=not self._url)
        check = check_url(self._url, allow_hosts=self._allow_hosts, resolve_dns=self._resolve_dns)
        if not check.allowed:
            logger.warning("alert webhook blocked by outbound guard: %s", check.reason)
            return AlertDispatchResult(failed=len(items), disabled=False)

        sent = failed = skipped = 0
        for item in items:
            key = f"{item.kind}:{item.target}:{item.state}"
            dispatch_id = await self._claim(key, item)
            if dispatch_id is None:
                skipped += 1
                continue
            ok, error = await self._post(item)
            await self._finish(dispatch_id, ok=ok, error=error)
            sent += int(ok)
            failed += int(not ok)
        if sent or failed:
            logger.info("alert dispatch sent=%d failed=%d skipped=%d", sent, failed, skipped)
        return AlertDispatchResult(sent=sent, skipped=skipped, failed=failed)

    async def _claim(self, key: str, item: AttentionItem) -> int | None:
        """先占唯一键再发送:抢不到说明别的副本已经处理过这条事项。"""
        async with get_async_session() as session:
            row = AlertDispatch(
                idempotency_key=key,
                kind=item.kind,
                target=item.target[:255],
                state=item.state[:32],
                ok=False,
            )
            session.add(row)
            try:
                await session.flush()
                dispatch_id = int(row.id)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
        return dispatch_id

    async def _finish(self, dispatch_id: int, *, ok: bool, error: str | None) -> None:
        async with get_async_session() as session, session.begin():
            row = await session.get(AlertDispatch, dispatch_id)
            if row is None:  # pragma: no cover - 刚写入就消失只可能是并发删除
                return
            row.ok = ok
            row.error = (error or "")[:255] or None

    async def _post(self, item: AttentionItem) -> tuple[bool, str | None]:
        payload = {
            "kind": item.kind,
            "target": item.target,
            "account_id": item.account_id,
            "state": item.state,
            "reason": item.reason,
            "next_command": item.next_command,
            "since": item.since.isoformat() if item.since else None,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}"
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        return True, None
