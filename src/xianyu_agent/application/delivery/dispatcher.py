"""OrderPaid 的分发:阶段 6 发货引擎与旧卡密链路的切换(Issue #5 Phase 6)。

配置了启用的发货策略时,订单事件走阶段 6 的执行器(校验 → 守卫 → 策略 → 预留 → 发送);
没有策略时保持旧卡密链路,避免"升级一上线就什么都不发"。engine 可以强制成 legacy 或 phase6,
auto 是默认值。

发送端口复用 worker 的消息发送函数:它返回 False 表示"这次没发成功",但请求可能已经出去,
因此按结果不明(UNCERTAIN)处理,而不是当作"明确没发"。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from xianyu_agent.application.delivery.executor import (
    DeliveryExecutor,
    DeliveryRequest,
    SenderPort,
    SendOutcome,
)
from xianyu_agent.application.delivery.policy_service import DeliveryPolicyService
from xianyu_agent.domain.events import OrderPaid

logger = logging.getLogger(__name__)

ENGINE_AUTO = "auto"
ENGINE_LEGACY = "legacy"
ENGINE_PHASE6 = "phase6"
ENGINES: tuple[str, ...] = (ENGINE_AUTO, ENGINE_LEGACY, ENGINE_PHASE6)

RawSender = Callable[[str, str, str], Awaitable[bool]]
LegacyDelivery = Callable[[OrderPaid], Awaitable[object]]


class OrderDeliveryDispatcher:
    """Route one OrderPaid event to the right delivery engine."""

    def __init__(
        self,
        *,
        sender: RawSender,
        engine: str = ENGINE_AUTO,
        policies: DeliveryPolicyService | None = None,
        executor: DeliveryExecutor | None = None,
        legacy_factory: Callable[[], LegacyDelivery] | None = None,
    ) -> None:
        if engine not in ENGINES:
            msg = f"未知发货引擎: {engine!r},可选 {ENGINES}"
            raise ValueError(msg)
        self._raw_sender = sender
        self._engine = engine
        self._policies = policies or DeliveryPolicyService()
        self._executor = executor
        self._legacy_factory = legacy_factory

    async def deliver(self, event: OrderPaid) -> str:
        """Deliver one paid order; returns which engine handled it."""
        engine = await self._resolve_engine()
        if engine == ENGINE_PHASE6:
            return await self._deliver_phase6(event)
        return await self._deliver_legacy(event)

    async def _resolve_engine(self) -> str:
        if self._engine != ENGINE_AUTO:
            return self._engine
        try:
            policies = await self._policies.list(only_enabled=True)
        except Exception as exc:
            logger.warning("发货策略读取失败,回退旧链路: %s", type(exc).__name__)
            return ENGINE_LEGACY
        return ENGINE_PHASE6 if policies else ENGINE_LEGACY

    async def _deliver_phase6(self, event: OrderPaid) -> str:
        executor = self._executor or DeliveryExecutor(sender=self._sender_port())
        result = await executor.execute(
            DeliveryRequest(
                account_id=event.account_id,
                order_id=event.order_id,
                item_id=event.item_id,
                buyer_id=event.buyer_id,
                buyer_name=event.buyer_name,
                item_title=event.item_title,
            )
        )
        logger.info(
            "订单发货(阶段6) account=%s order=%s decision=%s state=%s",
            event.account_id,
            event.order_id,
            result.decision,
            result.job.state,
        )
        return result.decision

    async def _deliver_legacy(self, event: OrderPaid) -> str:
        if self._legacy_factory is None:
            return "legacy_unavailable"
        result = await self._legacy_factory()(event)
        delivered = bool(getattr(result, "delivered", False))
        return "legacy_delivered" if delivered else "legacy_failed"

    def _sender_port(self) -> SenderPort:
        raw = self._raw_sender

        async def send(account_id: str, order_id: str, text: str) -> SendOutcome:
            try:
                sent = await raw(account_id, order_id, text)
            except Exception as exc:
                return SendOutcome.unknown(f"发送异常({type(exc).__name__})")
            if sent:
                return SendOutcome.success()
            return SendOutcome.unknown("发送通道返回失败,无法确认对方是否收到")

        return send
