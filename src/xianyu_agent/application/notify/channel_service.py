"""通知渠道的应用层:建渠道、绑账号、试发送。

密钥只以 SecretVault 引用出入:命令行与接口都只收/回引用名,明文由 `ChannelSender` 在出站前
那一刻才解开。试发送是一条真实出站请求,但目标由运营自己配置,且不接触闲鱼平台,因此不需要
平台校准门禁;仍然要过出站守卫。
"""

from __future__ import annotations

import logging
from dataclasses import replace

from sqlalchemy.exc import IntegrityError

from xianyu_agent.application.ports.secret_vault import SecretRef
from xianyu_agent.db import get_async_session
from xianyu_agent.domain.notify import (
    ChannelError,
    NotificationChannel,
    NotifyMessage,
)
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyNotificationChannelRepository,
)
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault

from .sender import ChannelSender, SendResult

logger = logging.getLogger(__name__)


def _vault() -> SqlAlchemySecretVault:
    return SqlAlchemySecretVault(FernetKeyring.from_environment())


async def reveal_secret(secret_ref: str | None) -> str | None:
    """取出渠道密钥;没配引用或引用查不到都返回 None,由调用方按配置错误处理。"""
    if not secret_ref:
        return None
    try:
        secret = await _vault().resolve(SecretRef(secret_ref))
    except Exception as exc:  # 密钥库异常种类多,统一降级成"取不到密钥"
        logger.warning("notification secret unavailable: %s", type(exc).__name__)
        return None
    return secret.reveal()


class NotificationChannelService:
    """通知渠道的读写与绑定。"""

    def __init__(self, *, sender: ChannelSender | None = None) -> None:
        self._sender = sender or ChannelSender()

    async def create(
        self,
        *,
        name: str,
        kind: str,
        target: str,
        secret_ref: str | None = None,
        events: tuple[str, ...] = (),
        enabled: bool = True,
        description: str | None = None,
    ) -> NotificationChannel:
        channel = NotificationChannel(
            name=name,
            kind=kind,
            target=target,
            secret_ref=secret_ref,
            events=events,
            enabled=enabled,
            description=description,
        )
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyNotificationChannelRepository(session)
            if await repo.get_by_name(channel.name) is not None:
                msg = f"通知渠道 {channel.name} 已存在"
                raise ChannelError(msg)
            channel_id = await repo.add(channel)
            stored = await repo.get(channel_id)
        if stored is None:  # pragma: no cover - 刚写入就消失只可能是并发删除
            msg = f"通知渠道 #{channel_id} 创建后读取失败"
            raise RuntimeError(msg)
        return stored

    async def get(self, channel_id: int) -> NotificationChannel:
        async with get_async_session() as session:
            channel = await SqlAlchemyNotificationChannelRepository(session).get(channel_id)
        if channel is None:
            msg = f"通知渠道 #{channel_id} 不存在"
            raise ValueError(msg)
        return channel

    async def resolve_ref(self, ref: str) -> NotificationChannel:
        """渠道引用可以写 id,也可以写名字。"""
        text = ref.strip()
        if not text:
            msg = "通知渠道引用不能为空"
            raise ValueError(msg)
        async with get_async_session() as session:
            repo = SqlAlchemyNotificationChannelRepository(session)
            if text.isdigit():
                channel = await repo.get(int(text))
                if channel is not None:
                    return channel
            named = await repo.get_by_name(text)
        if named is None:
            msg = f"找不到通知渠道 {text!r}"
            raise ValueError(msg)
        return named

    async def list_channels(self, *, only_enabled: bool = False) -> list[NotificationChannel]:
        async with get_async_session() as session:
            return await SqlAlchemyNotificationChannelRepository(session).list_channels(
                only_enabled=only_enabled
            )

    async def update(
        self,
        channel_id: int,
        *,
        name: str | None = None,
        target: str | None = None,
        secret_ref: str | None = None,
        events: tuple[str, ...] | None = None,
        enabled: bool | None = None,
        description: str | None = None,
    ) -> NotificationChannel:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyNotificationChannelRepository(session)
            current = await repo.get(channel_id)
            if current is None:
                msg = f"通知渠道 #{channel_id} 不存在"
                raise ValueError(msg)
            merged = replace(
                current,
                name=name if name is not None else current.name,
                target=target if target is not None else current.target,
                secret_ref=secret_ref if secret_ref is not None else current.secret_ref,
                events=events if events is not None else current.events,
                enabled=enabled if enabled is not None else current.enabled,
                description=description if description is not None else current.description,
            )
            validated = NotificationChannel(
                name=merged.name,
                kind=merged.kind,
                target=merged.target,
                secret_ref=merged.secret_ref,
                events=tuple(merged.events),
                enabled=merged.enabled,
                description=merged.description,
                local_id=merged.local_id,
            )
            try:
                await repo.apply(validated)
            except IntegrityError as exc:
                msg = f"通知渠道名 {validated.name} 已被占用"
                raise ChannelError(msg) from exc
        return validated

    async def set_enabled(self, channel_id: int, *, enabled: bool) -> NotificationChannel:
        return await self.update(channel_id, enabled=enabled)

    async def delete(self, channel_id: int) -> None:
        await self.get(channel_id)
        async with get_async_session() as session, session.begin():
            await SqlAlchemyNotificationChannelRepository(session).delete(channel_id)

    async def bind(self, channel_id: int, account_id: str) -> bool:
        async with get_async_session() as session, session.begin():
            return await SqlAlchemyNotificationChannelRepository(session).bind(
                channel_id, account_id
            )

    async def unbind(self, channel_id: int, account_id: str) -> bool:
        async with get_async_session() as session, session.begin():
            return await SqlAlchemyNotificationChannelRepository(session).unbind(
                channel_id, account_id
            )

    async def bound_accounts(self, channel_id: int) -> list[str]:
        async with get_async_session() as session:
            return await SqlAlchemyNotificationChannelRepository(session).bound_accounts(channel_id)

    async def test_send(self, channel_id: int) -> SendResult:
        """给这条渠道发一条固定文案的测试通知;只回成功或失败原因,不回地址与密钥。"""
        channel = await self.get(channel_id)
        message = NotifyMessage(
            event="account_risk",
            title="通知渠道测试",
            body="这是一条测试消息;收到即表示该渠道配置可用。",
            level="test",
        )
        secret = await reveal_secret(channel.secret_ref)
        return await self._sender.send(channel, message, secret=secret)
