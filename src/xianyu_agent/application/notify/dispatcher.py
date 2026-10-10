"""把待人工处理事项派发到已启用的通知渠道(对照 Helper 的 notifications/outbox)。

三条纪律:

1. **同一渠道同一条事项只发一次**:靠 `alert_dispatches` 的唯一键收敛,多副本同时扫描时只有
   一个会真正发出去;键里带渠道 id,所以新增渠道不会让旧渠道重复发送。
2. **不阻塞主循环**:任何失败只记日志与发送事实,绝不抛给 daemon。
3. **不外泄敏感信息**:载荷只含事项自身字段;密钥在出站前才从 SecretVault 解开,不经过日志。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.exc import IntegrityError

from xianyu_agent.application.attention.service import AttentionItem
from xianyu_agent.db import AlertDispatch, get_async_session
from xianyu_agent.domain.notify import NotificationChannel, NotifyMessage
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyNotificationChannelRepository,
)

from .channel_service import reveal_secret
from .sender import ChannelSender

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """一轮派发的结果:没有可用渠道、发了多少、跳过多少、失败多少。"""

    sent: int = 0
    skipped: int = 0
    failed: int = 0
    disabled: bool = False

    @property
    def enabled(self) -> bool:
        return not self.disabled


class NotificationDispatcher:
    """按渠道订阅关系与账号绑定派发事项,每条只发一次。"""

    ChannelProvider = Callable[[], Awaitable[tuple[list[NotificationChannel], dict[int, set[str]]]]]

    def __init__(
        self,
        *,
        sender: ChannelSender | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_hosts: tuple[str, ...] = (),
        resolve_dns: bool = True,
        channels_provider: ChannelProvider | None = None,
    ) -> None:
        self._sender = sender or ChannelSender(
            timeout_seconds=timeout_seconds,
            transport=transport,
            allow_hosts=allow_hosts,
            resolve_dns=resolve_dns,
        )
        self._channels_provider = channels_provider

    async def _load_channels(self) -> tuple[list[NotificationChannel], dict[int, set[str]]]:
        if self._channels_provider is not None:
            return await self._channels_provider()
        async with get_async_session() as session:
            repo = SqlAlchemyNotificationChannelRepository(session)
            channels = await repo.list_channels(only_enabled=True)
            if not channels:
                return [], {}
            bindings = await repo.bindings_by_channel()
        return channels, bindings

    async def dispatch(self, items: Sequence[AttentionItem]) -> DispatchResult:
        """派发这批事项;没有启用渠道时返回 disabled,不做任何网络请求。

        事项为空也要查一次渠道:调用方要靠 `disabled` 判断该不该回落到环境变量里的那个
        webhook,这个判断与"这一轮有没有新事项"无关。
        """
        try:
            channels, bindings = await self._load_channels()
        except Exception as exc:  # 通知是辅助能力,查库失败不能影响主循环
            logger.warning("notification channel lookup failed: %s", type(exc).__name__)
            return DispatchResult(failed=len(items))
        usable: list[tuple[NotificationChannel, int]] = [
            (channel, int(channel.local_id)) for channel in channels if channel.local_id is not None
        ]
        if not usable:
            return DispatchResult(disabled=True)

        sent = skipped = failed = 0
        for item in items:
            for channel, channel_id in usable:
                if not channel.accepts(item.kind):
                    skipped += 1
                    continue
                bound = bindings.get(channel_id, set())
                if bound and (item.account_id or "") not in bound:
                    skipped += 1
                    continue
                key = self._dispatch_key(channel_id, item)
                dispatch_id = await self._claim(key, item, channel)
                if dispatch_id is None:
                    skipped += 1
                    continue
                ok, error = await self._send(channel, item)
                await self._finish(dispatch_id, ok=ok, error=error)
                sent += int(ok)
                failed += int(not ok)
        if sent or failed:
            logger.info("notification dispatch sent=%d failed=%d skipped=%d", sent, failed, skipped)
        return DispatchResult(sent=sent, skipped=skipped, failed=failed)

    @staticmethod
    def _dispatch_key(channel_id: int, item: AttentionItem) -> str:
        return f"channel:{channel_id}:{item.kind}:{item.target}:{item.state}"[:255]

    async def _claim(
        self, key: str, item: AttentionItem, channel: NotificationChannel
    ) -> int | None:
        """先占唯一键再发送:抢不到说明这条事项已经有副本处理过。"""
        async with get_async_session() as session:
            row = AlertDispatch(
                idempotency_key=key,
                kind=item.kind,
                target=item.target[:255],
                state=item.state[:32],
                channel_id=channel.local_id,
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

    async def _send(
        self, channel: NotificationChannel, item: AttentionItem
    ) -> tuple[bool, str | None]:
        secret = await reveal_secret(channel.secret_ref)
        if channel.secret_ref and secret is None:
            return False, "SECRET_UNAVAILABLE"
        message = NotifyMessage(
            event=item.kind,
            title=self._title(item),
            body=self._body(item),
            level=item.state,
            account_id=item.account_id,
            target=item.target,
            occurred_at=item.since or datetime.now(UTC),
        )
        result = await self._sender.send(channel, message, secret=secret)
        return result.ok, result.error

    @staticmethod
    def _title(item: AttentionItem) -> str:
        labels = {
            "delivery_job": "发货任务需要人工处理",
            "automation_run": "平台自动化结果不明",
            "account_risk": "账号风控需要验证",
        }
        return labels.get(item.kind, f"待人工处理:{item.kind}")

    @staticmethod
    def _body(item: AttentionItem) -> str:
        lines = []
        if item.reason:
            lines.append(f"原因:{item.reason}")
        lines.append(f"收尾命令:{item.next_command}")
        return "\n".join(lines)
