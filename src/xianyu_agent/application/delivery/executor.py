"""发货执行器:校验 → 守卫 → 策略 → 预留 → 发送 → 收尾(Issue #5 Phase 6, §13/§15)。

这条链路把前面几个分片串起来,并且把"外部到底收到了没有"写进状态:

1. 发送前重新读订单(M18):只有 paid 状态才允许继续,已发货/已退款/已取消一律停下;
2. 守卫判定(fail closed):BLOCK 进 blocked,需要人工的进 needs_confirmation;
3. 来源解析:给了规格组合就按规格绑定取池与数量,否则用策略指向的池;
4. 原子预留:预留失败就是没库存,不发货;
5. 发送:逐条内容交给注入的发送端口,失败按 FailureKind 分类;
6. 收尾:明确成功才核销库存;明确没发出才退库存;结果不明一律冻结(§15)。

这个模块不直接连平台:发送动作由调用方注入(runtime 或 CLI),所以整条链路可以离线测试。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from xianyu_agent.application.delivery.api_config_service import ApiConfigService
from xianyu_agent.application.delivery.guard_service import DeliveryGuardService
from xianyu_agent.application.delivery.job_service import DeliveryJobService
from xianyu_agent.application.delivery.policy_service import DeliveryPolicyService
from xianyu_agent.application.delivery.spec_source import SpecSourceService
from xianyu_agent.application.delivery.template_service import DeliveryTemplateService
from xianyu_agent.application.inventory import InventoryReservationService, InventoryService
from xianyu_agent.db import Account, Order, get_async_session
from xianyu_agent.domain.delivery import (
    DeliveryDecision,
    DeliveryJob,
    DeliveryJobState,
    FailureKind,
    GuardContext,
    TemplateError,
    render_template,
)
from xianyu_agent.domain.delivery.policy import DeliveryPolicy
from xianyu_agent.domain.inventory import InventoryPool, InventoryPoolType, Reservation
from xianyu_agent.domain.item.sku import SpecValue
from xianyu_agent.infrastructure.delivery import ApiDeliveryProvider

logger = logging.getLogger(__name__)

ORDER_PAID = "paid"


@dataclass(frozen=True)
class SendOutcome:
    """What the transport layer can prove about one outbound message."""

    sent: bool
    external_message_id: str | None = None
    failure: FailureKind | None = None
    error: str | None = None

    @classmethod
    def success(cls, external_message_id: str | None = None) -> SendOutcome:
        return cls(sent=True, external_message_id=external_message_id)

    @classmethod
    def not_sent(cls, error: str) -> SendOutcome:
        return cls(sent=False, failure=FailureKind.NOT_SENT, error=error)

    @classmethod
    def rejected(cls, error: str) -> SendOutcome:
        return cls(sent=False, failure=FailureKind.REJECTED, error=error)

    @classmethod
    def unknown(cls, error: str) -> SendOutcome:
        return cls(sent=False, failure=FailureKind.UNKNOWN, error=error)


SenderPort = Callable[[str, str, str], Awaitable[SendOutcome]]
# 图片发送端口:(账号, 买家, 图片地址) -> 结果。第三个参数是 https 图片地址而不是文案。
ImageSenderPort = Callable[[str, str, str], Awaitable[SendOutcome]]


@dataclass(frozen=True)
class ConsignOutcome:
    """What the platform-confirmation call proved about one order.

    这个动作发生在内容已经发给买家之后,所以它不决定库存去留,只决定任务状态:
    确认成功才核销库存并收尾;明确被拒绝与结果不明都留给人工,绝不自动重发卡密。
    """

    confirmed: bool = False
    failure: FailureKind | None = None
    error: str | None = None

    @classmethod
    def success(cls) -> ConsignOutcome:
        return cls(confirmed=True)

    @classmethod
    def rejected(cls, error: str) -> ConsignOutcome:
        return cls(failure=FailureKind.REJECTED, error=error)

    @classmethod
    def unknown(cls, error: str) -> ConsignOutcome:
        return cls(failure=FailureKind.UNKNOWN, error=error)


# 端口签名:(账号, 订单, 发给买家的凭证文本) -> 平台确认结果。
ConsignPort = Callable[[str, str, str], Awaitable[ConsignOutcome]]


@dataclass(frozen=True)
class OrderCheck:
    """Result of re-reading the order right before any external action."""

    allowed: bool
    reason: str
    status: str | None = None
    item_id: str | None = None
    buyer_id: str | None = None
    buyer_name: str | None = None
    item_title: str | None = None
    quantity: int = 1


@dataclass(frozen=True)
class DeliveryRequest:
    account_id: str
    order_id: str
    item_id: str | None = None
    buyer_id: str | None = None
    buyer_name: str | None = None
    item_title: str | None = None
    sku_ref: str | None = None
    selection: Mapping[str, str] | Sequence[SpecValue] | None = None
    quantity: int | None = None
    respect_delay: bool = True


@dataclass(frozen=True)
class DeliveryExecutionResult:
    job: DeliveryJob
    decision: str
    reason: str
    reservation_id: int | None = None
    pool_id: int | None = None
    contents_sent: int = 0


@dataclass(frozen=True)
class DeliveryPlan:
    """Dry Run 结果:这次发货会怎么走,不产生任何状态变化。"""

    allowed: bool
    reason: str
    order_status: str | None = None
    guard_decision: str | None = None
    guard_reason: str = ""
    policy_name: str | None = None
    policy_level: str | None = None
    pool_id: int | None = None
    quantity: int = 0
    delay_seconds: int = 0
    platform_confirm: bool = False
    template: str | None = None
    hit_rules: tuple[str, ...] = ()


class DeliveryOrderValidator:
    """Re-read the order mirror and decide whether delivery may proceed (M18)."""

    async def check(self, account_id: str, order_id: str) -> OrderCheck:
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return OrderCheck(allowed=False, reason=f"账号 {account_id} 不存在")
            order = (
                await session.execute(
                    select(Order)
                    .where(Order.account_id == account.id, Order.order_id == order_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        if order is None:
            return OrderCheck(allowed=False, reason=f"订单 {order_id} 不在本地镜像里")
        snapshot = OrderCheck(
            allowed=True,
            reason="订单已付款,可以发货",
            status=order.status,
            item_id=order.item_id,
            buyer_id=order.buyer_id,
            buyer_name=order.buyer_name,
            item_title=order.item_title,
            quantity=int(order.quantity or 1),
        )
        if order.status == ORDER_PAID:
            return snapshot
        if order.status in {"delivered", "completed"}:
            return OrderCheck(allowed=False, reason="订单已发货,不重复履约", status=order.status)
        if order.status in {"cancelled", "refunded"}:
            return OrderCheck(
                allowed=False,
                reason=f"订单状态为 {order.status},不能自动发货",
                status=order.status,
            )
        return OrderCheck(
            allowed=False, reason=f"订单状态为 {order.status},还不是已付款", status=order.status
        )


class _StopDelivery(Exception):
    """内部信号:某一阶段已经形成终局结果,直接返回给调用方。"""

    def __init__(self, result: DeliveryExecutionResult) -> None:
        super().__init__(result.reason)
        self.result = result


class DeliveryExecutor:
    """Run the full delivery pipeline for one order."""

    def __init__(
        self,
        sender: SenderPort | None = None,
        image_sender: ImageSenderPort | None = None,
        *,
        jobs: DeliveryJobService | None = None,
        policies: DeliveryPolicyService | None = None,
        guards: DeliveryGuardService | None = None,
        reservations: InventoryReservationService | None = None,
        inventory: InventoryService | None = None,
        spec_source: SpecSourceService | None = None,
        api_provider: ApiDeliveryProvider | None = None,
        api_configs: ApiConfigService | None = None,
        templates: DeliveryTemplateService | None = None,
        consign: ConsignPort | None = None,
        validator: DeliveryOrderValidator | None = None,
        waiter: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._sender = sender
        self._image_sender = image_sender
        self._jobs = jobs or DeliveryJobService()
        self._policies = policies or DeliveryPolicyService()
        self._guards = guards or DeliveryGuardService()
        self._reservations = reservations or InventoryReservationService()
        self._inventory = inventory or InventoryService()
        self._spec_source = spec_source or SpecSourceService(reservations=self._reservations)
        self._api_provider = api_provider
        self._api_configs = api_configs or ApiConfigService()
        self._templates = templates or DeliveryTemplateService()
        self._consign = consign
        self._validator = validator or DeliveryOrderValidator()
        self._wait = waiter or asyncio.sleep
        self._job_id = 0
        self._check = OrderCheck(allowed=False, reason="未校验")
        self._context = GuardContext(account_id="", order_id="")
        # 已经发给买家的内容:平台确认时要把它作为发货凭证带回去,但绝不落日志。
        self._sent_contents: tuple[str, ...] = ()
        # 模板库引用按任务解析一次,避免同一订单的多次渲染重复查库。
        self._resolved_template: str | None = None
        # 实际生效的等待秒数(池级与策略级取较大者),在加载库存池时确定。
        self._delay_seconds = 0

    async def _effective_template(self, policy: DeliveryPolicy) -> str | None:
        """内联模板优先;没有内联模板时才去模板库解析 template_ref。"""
        if policy.template is not None:
            return policy.template
        if not policy.template_ref:
            return None
        if self._resolved_template is None:
            template = await self._templates.resolve_ref(policy.template_ref)
            if not template.enabled:
                msg = f"发货模板 {template.name} 已停用,不能用于发货"
                raise ValueError(msg)
            self._resolved_template = template.content
        return self._resolved_template

    def _pool_scope_block_reason(
        self, pool: InventoryPool, *, sku_ref: str | None
    ) -> str | None:
        """库存池的范围是安全边界,必须在发货前真的校验,而不是只显示在列表里。

        账号/商品/规格任一范围与本次请求不符就直接拒绝:把池给错账号发货,比不发更糟。
        """
        if pool.account_scope and pool.account_scope != self._context.account_id:
            return (
                f"库存池 #{pool.local_id} 限定账号 {pool.account_scope},"
                f"本次请求账号是 {self._context.account_id}"
            )
        item_id = self._check.item_id
        if pool.item_scope and pool.item_scope != item_id:
            return (
                f"库存池 #{pool.local_id} 限定商品 {pool.item_scope},"
                f"本次订单商品是 {item_id or '未知'}"
            )
        if pool.sku_scope and pool.sku_scope != sku_ref:
            return (
                f"库存池 #{pool.local_id} 限定规格 {pool.sku_scope},"
                f"本次请求规格是 {sku_ref or '未指定'}"
            )
        return None

    @staticmethod
    def _effective_delay(policy: DeliveryPolicy, pool: InventoryPool) -> int:
        """池级延迟与策略级延迟都表示"至少等这么久",取较大者而不是相加。

        原先池级 delay_seconds 只在列表里显示、发货时被忽略,属于声明了却不生效的配置。
        """
        return max(0, policy.delay_seconds, pool.delay_seconds or 0)

    async def execute(self, request: DeliveryRequest) -> DeliveryExecutionResult:
        """Run the pipeline; every phase may stop with a fully-formed result."""
        try:
            job = await self._open_job(request)
            policy = await self._preflight(request)
            if self._sender is None and self._image_sender is None:
                # 一个发送端口都没有就不该占库存:直接停下,由调用方补端口或改用 plan().
                # 只有图片端口时,文本池会在自己的分支上失败,而不是在这里被拦下。
                raise _StopDelivery(  # noqa: TRY301 - 阶段内直接返回终局结果
                    DeliveryExecutionResult(
                        job=job,
                        decision=DeliveryDecision.REQUIRE_CONFIRMATION.value,
                        reason="未配置发送端口:未预留,也未发送任何内容",
                    )
                )
            pool_id, quantity = await self._plan_source(request, policy, job)
            pool = await self._inventory.load(pool_id)
            scope_block = self._pool_scope_block_reason(
                pool, sku_ref=policy.sku_id or request.sku_ref
            )
            if scope_block is not None:
                blocked = await self._jobs.set_state(
                    self._job_id, DeliveryJobState.BLOCKED.value, last_error=scope_block
                )
                raise _StopDelivery(  # noqa: TRY301 - 阶段内直接返回终局结果
                    DeliveryExecutionResult(
                        job=blocked,
                        decision=DeliveryDecision.BLOCK.value,
                        reason=scope_block,
                        pool_id=pool.local_id,
                    )
                )
            self._delay_seconds = self._effective_delay(policy, pool)
            if pool.type == InventoryPoolType.API.value:
                raise _StopDelivery(  # noqa: TRY301 - 阶段内直接返回终局结果
                    await self._execute_api(request, policy, pool, quantity)
                )
            if pool.is_literal:
                raise _StopDelivery(  # noqa: TRY301 - 阶段内直接返回终局结果
                    await self._execute_literal(request, policy, pool)
                )
            reservation, pool_id, quantity = await self._reserve(pool_id, quantity)
            sent_count = await self._deliver(request, policy, reservation, quantity, pool_id)
        except _StopDelivery as stop:
            return stop.result
        return await self._complete(policy, reservation, pool_id, sent_count)

    async def plan(self, request: DeliveryRequest) -> DeliveryPlan:
        """Dry Run:校验订单、跑守卫、解析策略,但不预留也不发送。"""
        check = await self._validator.check(request.account_id, request.order_id)
        if not check.allowed:
            return DeliveryPlan(allowed=False, reason=check.reason, order_status=check.status)
        context = GuardContext(
            account_id=request.account_id,
            order_id=request.order_id,
            item_id=request.item_id or check.item_id,
            buyer_id=request.buyer_id or check.buyer_id,
            buyer_name=request.buyer_name or check.buyer_name,
        )
        # Dry Run 后续也复用范围校验,必须把本次订单上下文写入执行器。
        # 否则 account_scope/item_scope 会拿到初始化时的空上下文,误拦截合法策略。
        self._check = check
        self._context = context
        guard = await self._guards.evaluate(context)
        if guard.decision != DeliveryDecision.ALLOW.value:
            return DeliveryPlan(
                allowed=False,
                reason=guard.reason,
                order_status=check.status,
                guard_decision=guard.decision,
                guard_reason=guard.reason,
                hit_rules=guard.hit_rule_codes(),
            )
        resolution = await self._policies.resolve(
            account_id=request.account_id,
            item_id=request.item_id or check.item_id,
            sku_id=request.sku_ref,
        )
        if resolution.policy is None:
            return DeliveryPlan(
                allowed=False,
                reason=resolution.reason,
                order_status=check.status,
                guard_decision=guard.decision,
                guard_reason=guard.reason,
            )
        policy = resolution.policy
        quantity = request.quantity or check.quantity
        pool_id = policy.inventory_pool_id
        if request.selection:
            spec_plan = await self._spec_source.plan(
                request.account_id, request.item_id or check.item_id or "", request.selection
            )
            if not spec_plan.matched or spec_plan.target_id is None:
                return DeliveryPlan(
                    allowed=False,
                    reason=f"规格组合没有可用的库存来源: {spec_plan.reason}",
                    order_status=check.status,
                    guard_decision=guard.decision,
                )
            pool_id, quantity = spec_plan.target_id, spec_plan.quantity
        pool = await self._inventory.load(pool_id)
        scope_block = self._pool_scope_block_reason(
            pool, sku_ref=policy.sku_id or request.sku_ref
        )
        if scope_block is not None:
            return DeliveryPlan(
                allowed=False,
                reason=scope_block,
                order_status=check.status,
                guard_decision=guard.decision,
                pool_id=pool_id,
            )
        # text / image 池的内容就是池本身,一笔订单只发一次;数量只对 data / api 池有意义。
        if await self._is_literal_pool(pool_id):
            quantity = 1
        return DeliveryPlan(
            allowed=True,
            reason="可以自动发货",
            order_status=check.status,
            guard_decision=guard.decision,
            guard_reason=guard.reason,
            policy_name=policy.name,
            policy_level=resolution.level,
            pool_id=pool_id,
            quantity=max(1, quantity),
            delay_seconds=self._effective_delay(policy, pool),
            platform_confirm=policy.platform_confirm,
            template=policy.template or policy.template_ref,
        )

    def _job_ref(self) -> str:
        return f"delivery-job:{self._job_id}"

    async def _open_job(self, request: DeliveryRequest) -> DeliveryJob:
        check = await self._validator.check(request.account_id, request.order_id)
        job = await self._jobs.create_or_get(
            account_id=request.account_id,
            order_id=request.order_id,
            required_quantity=request.quantity or check.quantity,
            item_id=request.item_id or check.item_id,
            sku_ref=request.sku_ref,
        )
        if job.local_id is None:  # pragma: no cover - create_or_get 一定带 id
            msg = "发货任务缺少 id"
            raise RuntimeError(msg)
        self._job_id = job.local_id
        if job.state == DeliveryJobState.COMPLETED.value:
            raise _StopDelivery(
                DeliveryExecutionResult(
                    job=job, decision="already_done", reason="该订单已经发货完成"
                )
            )
        # 先进入 checking 再决定放不放行:`_stop` 要把任务落成 blocked,而状态机里 blocked 只能
        # 从 checking 到达。顺序反了的话,"订单不在镜像里/已退款/还没付款"这类最常见的拒绝会在
        # 这里抛 InvalidStateTransition,把一个正常的业务拒绝变成带堆栈的内部错误。
        if job.state != DeliveryJobState.CHECKING.value:
            job = await self._jobs.set_state(job.local_id, DeliveryJobState.CHECKING.value)
        if not check.allowed:
            raise _StopDelivery(await self._stop(check, DeliveryJobState.BLOCKED.value))
        self._check = check
        return job

    async def _stop(self, check: OrderCheck, state: str) -> DeliveryExecutionResult:
        stopped = await self._jobs.set_state(self._job_id, state, last_error=check.reason)
        return DeliveryExecutionResult(
            job=stopped, decision=DeliveryDecision.BLOCK.value, reason=check.reason
        )

    async def _preflight(self, request: DeliveryRequest) -> DeliveryPolicy:
        check = self._check
        context = GuardContext(
            account_id=request.account_id,
            order_id=request.order_id,
            item_id=request.item_id or check.item_id,
            buyer_id=request.buyer_id or check.buyer_id,
            buyer_name=request.buyer_name or check.buyer_name,
        )
        self._context = context
        guard = await self._guards.evaluate(context)
        if guard.decision in {
            DeliveryDecision.BLOCK.value,
            DeliveryDecision.REQUIRE_CONFIRMATION.value,
        }:
            target = (
                DeliveryJobState.BLOCKED.value
                if guard.decision == DeliveryDecision.BLOCK.value
                else DeliveryJobState.NEEDS_CONFIRMATION.value
            )
            stopped = await self._jobs.set_state(self._job_id, target, last_error=guard.reason)
            raise _StopDelivery(
                DeliveryExecutionResult(job=stopped, decision=guard.decision, reason=guard.reason)
            )
        resolution = await self._policies.resolve(
            account_id=request.account_id,
            item_id=request.item_id or check.item_id,
            sku_id=request.sku_ref,
        )
        if resolution.policy is None:
            stopped = await self._jobs.set_state(
                self._job_id, DeliveryJobState.BLOCKED.value, last_error=resolution.reason
            )
            raise _StopDelivery(
                DeliveryExecutionResult(
                    job=stopped, decision=DeliveryDecision.BLOCK.value, reason=resolution.reason
                )
            )
        return resolution.policy

    async def _reserve(self, pool_id: int, quantity: int) -> tuple[Reservation, int, int]:
        await self._jobs.set_state(self._job_id, DeliveryJobState.RESERVING.value)
        try:
            reservation = await self._reservations.reserve(pool_id, self._job_ref(), quantity)
        except Exception as exc:
            failed = await self._jobs.register_failure(
                self._job_id, FailureKind.NOT_SENT, error=str(exc), action="reserve"
            )
            raise _StopDelivery(
                DeliveryExecutionResult(
                    job=failed,
                    decision=DeliveryDecision.REQUIRE_CONFIRMATION.value,
                    reason=str(exc),
                    pool_id=pool_id,
                )
            ) from exc
        await self._jobs.record_attempt(self._job_id, action="reserve", result="success")
        await self._jobs.attach_reservation(self._job_id, reservation_id_of(reservation))
        await self._jobs.set_state(self._job_id, DeliveryJobState.RESERVED.value)
        await self._jobs.set_state(self._job_id, DeliveryJobState.DELIVERING.value)
        return reservation, pool_id, quantity

    async def _deliver(
        self,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
        reservation: Reservation,
        quantity: int,
        pool_id: int,
    ) -> int:
        rendered = await self._render_contents(
            policy=policy,
            reservation=reservation,
            check=self._check,
            context=self._context,
            quantity=quantity,
        )
        if not rendered:
            await self._reconcile_inventory(
                self._job_id, reservation_id_of(reservation), FailureKind.REJECTED
            )
            failed = await self._jobs.register_failure(
                self._job_id,
                FailureKind.REJECTED,
                error="没有可发送的卡密内容",
                action="render",
            )
            raise _StopDelivery(
                DeliveryExecutionResult(
                    job=failed,
                    decision=DeliveryDecision.BLOCK.value,
                    reason="没有可发送的卡密内容",
                )
            )
        return await self._send_contents(
            request,
            rendered,
            reservation_id=reservation_id_of(reservation),
            pool_id=pool_id,
            delay_seconds=self._delay_seconds if request.respect_delay else 0,
        )

    async def _complete(
        self,
        policy: DeliveryPolicy,
        reservation: Reservation,
        pool_id: int,
        sent_count: int,
    ) -> DeliveryExecutionResult:
        confirming = await self._jobs.set_state(self._job_id, DeliveryJobState.CONFIRMING.value)
        if policy.platform_confirm:
            return await self._confirm_with_platform(
                confirming,
                reservation_id=reservation.local_id,
                pool_id=pool_id,
                contents_sent=sent_count,
            )
        await self._reservations.consume(reservation_id_of(reservation))
        return await self._finalize(
            reservation_id=reservation.local_id,
            pool_id=pool_id,
            contents_sent=sent_count,
            reason="发货完成",
        )

    async def _finalize(
        self,
        *,
        reservation_id: int | None,
        pool_id: int,
        contents_sent: int,
        reason: str,
    ) -> DeliveryExecutionResult:
        """内容已确认送达后收尾:任务完成,并把订单镜像标记为已发货。"""
        completed = await self._jobs.set_state(self._job_id, DeliveryJobState.COMPLETED.value)
        await self._record_order_outcome(
            self._context.account_id, self._context.order_id, delivered=True, error=None
        )
        return DeliveryExecutionResult(
            job=completed,
            decision=DeliveryDecision.ALLOW.value,
            reason=reason,
            reservation_id=reservation_id,
            pool_id=pool_id,
            contents_sent=contents_sent,
        )

    async def _confirm_with_platform(
        self,
        job: DeliveryJob,
        *,
        reservation_id: int | None,
        pool_id: int,
        contents_sent: int,
    ) -> DeliveryExecutionResult:
        """内容已经发给买家之后,再在订单上做平台确认发货。

        策略打了平台确认标记却没注入端口时保持原有安全行为:停在 confirming 等人工处理,
        不假装已经确认成功。
        """
        if self._consign is None:
            return DeliveryExecutionResult(
                job=job,
                decision=DeliveryDecision.REQUIRE_CONFIRMATION.value,
                reason="已发送,等待平台确认动作(未配置平台确认端口)",
                reservation_id=reservation_id,
                pool_id=pool_id,
                contents_sent=contents_sent,
            )
        credential = "\n".join(self._sent_contents)
        try:
            outcome = await self._consign(
                self._context.account_id, self._context.order_id, credential
            )
        except Exception as exc:  # 端口抛异常时按结果不明处理,绝不当作已确认
            logger.warning(
                "platform confirm raised account=%s order=%s",
                self._context.account_id,
                self._context.order_id,
            )
            outcome = ConsignOutcome.unknown(f"平台确认调用异常({type(exc).__name__})")
        if not outcome.confirmed:
            kind = outcome.failure or FailureKind.UNKNOWN
            if kind not in {FailureKind.REJECTED, FailureKind.UNKNOWN}:
                kind = FailureKind.UNKNOWN
            error = outcome.error or "平台确认发货失败"
            failed = await self._jobs.register_failure(
                self._job_id, kind, error=error, action="platform_confirm"
            )
            return DeliveryExecutionResult(
                job=failed,
                decision=DeliveryDecision.REQUIRE_CONFIRMATION.value,
                reason=f"内容已发给买家,但平台确认发货未完成: {error}",
                reservation_id=reservation_id,
                pool_id=pool_id,
                contents_sent=contents_sent,
            )
        await self._jobs.record_attempt(
            self._job_id, action="platform_confirm", result="success"
        )
        if reservation_id is not None:
            await self._reservations.consume(reservation_id)
        return await self._finalize(
            reservation_id=reservation_id,
            pool_id=pool_id,
            contents_sent=contents_sent,
            reason="发货完成(已确认平台发货)",
        )

    async def _send_contents(
        self,
        request: DeliveryRequest,
        rendered: Sequence[str],
        *,
        reservation_id: int | None,
        pool_id: int,
        delay_seconds: int = 0,
    ) -> int:
        """把渲染好的内容逐条发出去;失败时按外部副作用分类并收尾。"""
        sender = self._sender
        if sender is None:  # pragma: no cover - execute() 已经拦下没有发送端口的情况
            msg = "未配置发送端口"
            raise RuntimeError(msg)
        if delay_seconds:
            await self._wait(float(delay_seconds))
        sent_count = 0
        self._sent_contents = ()
        for content in rendered:
            outcome = await sender(self._context.account_id, request.order_id, content)
            if outcome.sent:
                sent_count += 1
                self._sent_contents = (*self._sent_contents, content)
                await self._jobs.record_attempt(
                    self._job_id,
                    action="send",
                    result="success",
                    external_message_id=outcome.external_message_id,
                )
                continue
            kind = outcome.failure or FailureKind.UNKNOWN
            error = outcome.error or "发送失败"
            if reservation_id is not None:
                await self._reconcile_inventory(self._job_id, reservation_id, kind)
            failed = await self._jobs.register_failure(
                self._job_id, kind, error=error, action="send"
            )
            await self._record_order_outcome(
                self._context.account_id, self._context.order_id, delivered=False, error=error
            )
            raise _StopDelivery(
                DeliveryExecutionResult(
                    job=failed,
                    decision=(
                        DeliveryDecision.BLOCK.value
                        if kind in {FailureKind.REJECTED, FailureKind.BLOCKED}
                        else DeliveryDecision.REQUIRE_CONFIRMATION.value
                    ),
                    reason=error,
                    reservation_id=reservation_id,
                    pool_id=pool_id,
                    contents_sent=sent_count,
                )
            )
        return sent_count

    async def _is_literal_pool(self, pool_id: int) -> bool:
        try:
            pool = await self._inventory.load(pool_id)
        except ValueError:
            # Dry Run 不该因为池缺失而报错,真正的失败留给 execute() 的加载步骤。
            return False
        return pool.is_literal

    async def _finish_without_reservation(
        self,
        policy: DeliveryPolicy,
        pool_id: int,
        sent_count: int,
        *,
        reason: str,
    ) -> DeliveryExecutionResult:
        """策略型池没有库存可核销:发送成功即收尾(平台确认另算)。"""
        confirming = await self._jobs.set_state(self._job_id, DeliveryJobState.CONFIRMING.value)
        if policy.platform_confirm:
            return await self._confirm_with_platform(
                confirming,
                reservation_id=None,
                pool_id=pool_id,
                contents_sent=sent_count,
            )
        return await self._finalize(
            reservation_id=None,
            pool_id=pool_id,
            contents_sent=sent_count,
            reason=reason,
        )

    async def _execute_literal(
        self,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
        pool: InventoryPool,
    ) -> DeliveryExecutionResult:
        """text / image 池:内容就在池里,不占用任何库存单元。

        文本只发一条:同一句话重复发给买家没有意义,数量只在 data / api 池表示要取几份。
        """
        if not pool.enabled:
            reason = f"库存池 #{pool.local_id} 已停用,不能自动发货"
            stopped = await self._jobs.set_state(
                self._job_id, DeliveryJobState.BLOCKED.value, last_error=reason
            )
            return DeliveryExecutionResult(
                job=stopped,
                decision=DeliveryDecision.BLOCK.value,
                reason=reason,
                pool_id=pool.local_id,
            )
        if pool.type == InventoryPoolType.IMAGE.value:
            return await self._execute_image(request, policy, pool)
        content = (pool.content or "").strip()
        if not content:
            reason = f"库存池 #{pool.local_id} 没有可发送的文案"
            failed = await self._jobs.register_failure(
                self._job_id, FailureKind.BLOCKED, error=reason, action="render"
            )
            return DeliveryExecutionResult(
                job=failed,
                decision=DeliveryDecision.BLOCK.value,
                reason=reason,
                pool_id=pool.local_id,
            )
        template = await self._effective_template(policy)
        if template is None:
            rendered = content
        else:
            try:
                rendered = render_template(
                    template,
                    {
                        "buyer_name": self._context.buyer_name or "",
                        "item_title": self._check.item_title or "",
                        "sku_name": policy.sku_id or "",
                        "order_id": self._context.order_id,
                        "quantity": "1",
                        # 池内容顶替 data 池里"取到的那条卡密"的位置。
                        "card": content,
                    },
                )
            except TemplateError as exc:
                failed = await self._jobs.register_failure(
                    self._job_id, FailureKind.BLOCKED, error=str(exc), action="render"
                )
                return DeliveryExecutionResult(
                    job=failed,
                    decision=DeliveryDecision.BLOCK.value,
                    reason=str(exc),
                    pool_id=pool.local_id,
                )
        # 状态机要求经 reserving 再到 delivering;策略型池没有单元,但状态路径保持一致。
        await self._jobs.set_state(self._job_id, DeliveryJobState.RESERVING.value)
        await self._jobs.set_state(self._job_id, DeliveryJobState.DELIVERING.value)
        sent_count = await self._send_contents(
            request,
            (rendered,),
            reservation_id=None,
            pool_id=pool.local_id or 0,
            delay_seconds=self._delay_seconds if request.respect_delay else 0,
        )
        return await self._finish_without_reservation(
            policy, pool.local_id or 0, sent_count, reason="发货完成(内容来自库存池)"
        )

    async def _execute_image(
        self,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
        pool: InventoryPool,
    ) -> DeliveryExecutionResult:
        """image 池:把图片当图片发,而不是把地址当文本发。

        没有注入图片端口时保持原有安全行为(直接 BLOCK):发图协议虽然按参考项目实现,
        但没有真实校准前不应该假装能发。
        """
        if not pool.enabled:
            reason = f"库存池 #{pool.local_id} 已停用,不能自动发货"
            stopped = await self._jobs.set_state(
                self._job_id, DeliveryJobState.BLOCKED.value, last_error=reason
            )
            return DeliveryExecutionResult(
                job=stopped,
                decision=DeliveryDecision.BLOCK.value,
                reason=reason,
                pool_id=pool.local_id,
            )
        image_url = (pool.image_url or "").strip()
        if not image_url:
            reason = f"库存池 #{pool.local_id} 没有可发送的图片地址"
            failed = await self._jobs.register_failure(
                self._job_id, FailureKind.BLOCKED, error=reason, action="render"
            )
            return DeliveryExecutionResult(
                job=failed,
                decision=DeliveryDecision.BLOCK.value,
                reason=reason,
                pool_id=pool.local_id,
            )
        if self._image_sender is None:
            reason = (
                "image 类型库存池需要图片发送端口,当前调用方没有注入;"
                "请改用 text 池,或由 daemon/CLI 注入端口"
            )
            failed = await self._jobs.register_failure(
                self._job_id, FailureKind.BLOCKED, error=reason, action="send"
            )
            return DeliveryExecutionResult(
                job=failed,
                decision=DeliveryDecision.BLOCK.value,
                reason=reason,
                pool_id=pool.local_id,
            )
        receiver = request.buyer_id or self._check.buyer_id
        if not receiver:
            reason = "订单镜像里没有买家标识,无法定位会话发送图片"
            failed = await self._jobs.register_failure(
                self._job_id, FailureKind.NOT_SENT, error=reason, action="send"
            )
            return DeliveryExecutionResult(
                job=failed,
                decision=DeliveryDecision.REQUIRE_CONFIRMATION.value,
                reason=reason,
                pool_id=pool.local_id,
            )
        await self._jobs.set_state(self._job_id, DeliveryJobState.RESERVING.value)
        await self._jobs.set_state(self._job_id, DeliveryJobState.DELIVERING.value)
        outcome = await self._image_sender(request.account_id, receiver, image_url)
        if not outcome.sent:
            kind = outcome.failure or FailureKind.UNKNOWN
            error = outcome.error or "图片发送失败"
            failed = await self._jobs.register_failure(
                self._job_id, kind, error=error, action="send"
            )
            await self._record_order_outcome(
                self._context.account_id, self._context.order_id, delivered=False, error=error
            )
            return DeliveryExecutionResult(
                job=failed,
                decision=(
                    DeliveryDecision.BLOCK.value
                    if kind in {FailureKind.REJECTED, FailureKind.BLOCKED}
                    else DeliveryDecision.REQUIRE_CONFIRMATION.value
                ),
                reason=error,
                pool_id=pool.local_id,
            )
        # 图片凭证参与平台确认:与文本凭证一起交给 consign。
        self._sent_contents = (*self._sent_contents, image_url)
        await self._jobs.record_attempt(
            self._job_id,
            action="send",
            result="success",
            external_message_id=outcome.external_message_id,
        )
        return await self._finish_without_reservation(
            policy, pool.local_id or 0, 1, reason="发货完成(图片来自库存池)"
        )

    async def _execute_api(
        self,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
        pool: InventoryPool,
        quantity: int,
    ) -> DeliveryExecutionResult:
        """api 池:卡密来自外部接口,本地没有库存可预留。"""
        try:
            config = await self._api_configs.resolve_ref(pool.api_config_ref or "")
        except ValueError as exc:
            failed = await self._jobs.register_failure(
                self._job_id, FailureKind.BLOCKED, error=str(exc), action="api_config"
            )
            return DeliveryExecutionResult(
                job=failed,
                decision=DeliveryDecision.BLOCK.value,
                reason=str(exc),
                pool_id=pool.local_id,
            )
        provider = self._api_provider or ApiDeliveryProvider.for_config(config)
        variables = {
            "order_id": self._context.order_id,
            "buyer_name": self._context.buyer_name or "",
            "item_title": self._check.item_title or "",
            "sku_name": policy.sku_id or "",
            "quantity": str(quantity),
        }
        await self._jobs.set_state(self._job_id, DeliveryJobState.RESERVING.value)
        contents: list[str] = []
        for _ in range(max(1, quantity)):
            fetch = await provider.fetch(config, variables)
            if fetch.content is None:
                kind = FailureKind.UNKNOWN if fetch.dispatched else FailureKind.NOT_SENT
                error = fetch.error or "取卡失败"
                failed = await self._jobs.register_failure(
                    self._job_id, kind, error=error, action="api_fetch"
                )
                await self._record_order_outcome(
                    self._context.account_id, self._context.order_id, delivered=False, error=error
                )
                return DeliveryExecutionResult(
                    job=failed,
                    decision=DeliveryDecision.REQUIRE_CONFIRMATION.value,
                    reason=error,
                    pool_id=pool.local_id,
                    contents_sent=len(contents),
                )
            await self._jobs.record_attempt(self._job_id, action="api_fetch", result="success")
            contents.append(fetch.content)
        await self._jobs.set_state(self._job_id, DeliveryJobState.DELIVERING.value)
        sent_count = await self._send_contents(
            request,
            tuple(contents),
            reservation_id=None,
            pool_id=pool.local_id or 0,
            delay_seconds=self._delay_seconds if request.respect_delay else 0,
        )
        return await self._finish_without_reservation(
            policy,
            pool.local_id or 0,
            sent_count,
            reason="发货完成(卡密来自外部接口)",
        )

    async def _plan_source(
        self, request: DeliveryRequest, policy: DeliveryPolicy, job: DeliveryJob
    ) -> tuple[int, int]:
        """按规格绑定或策略决定从哪个池取几份。"""
        if request.selection:
            plan = await self._spec_source.plan(
                # 用任务里已经解析过的商品 id:调用方可能没显式传 item_id。
                request.account_id,
                job.item_id or "",
                request.selection,
            )
            if not plan.matched or plan.target_id is None:
                msg = f"规格组合没有可用的库存来源: {plan.reason}"
                raise ValueError(msg)
            return plan.target_id, plan.quantity
        quantity = request.quantity or job.required_quantity
        return policy.inventory_pool_id, max(1, quantity)

    async def _render_contents(
        self,
        *,
        policy: DeliveryPolicy,
        reservation: Reservation,
        check: OrderCheck,
        context: GuardContext,
        quantity: int,
    ) -> tuple[str, ...]:
        codes = await self._reservations.contents(reservation.local_id or 0)
        rendered: list[str] = []
        template = await self._effective_template(policy)
        for code in codes:
            if template is None:
                rendered.append(code)
                continue
            rendered.append(
                render_template(
                    template,
                    {
                        "buyer_name": context.buyer_name or "",
                        "item_title": check.item_title or "",
                        "sku_name": policy.sku_id or "",
                        "order_id": context.order_id,
                        "quantity": str(quantity),
                        "card": code,
                    },
                )
            )
        return tuple(rendered)

    async def _reconcile_inventory(
        self, job_id: int, reservation_id: int | None, kind: FailureKind
    ) -> None:
        """按 §15 的表处理库存:明确没发才退,结果不明就冻结。"""
        if reservation_id is None:
            return
        try:
            if kind is FailureKind.UNKNOWN:
                await self._reservations.mark_uncertain(reservation_id)
            else:
                await self._reservations.release(reservation_id)
        except Exception as exc:
            logger.warning(
                "库存收尾失败 job=%s reservation=%s kind=%s: %s",
                job_id,
                reservation_id,
                kind,
                exc,
            )

    async def _record_order_outcome(
        self, account_id: str, order_id: str, *, delivered: bool, error: str | None
    ) -> None:
        async with get_async_session() as session, session.begin():
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                return
            order = (
                await session.execute(
                    select(Order)
                    .where(Order.account_id == account.id, Order.order_id == order_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if order is None:
                return
            if delivered:
                order.status = "delivered"
                order.delivered_at = datetime.now(UTC)
                order.delivery_fail_reason = None
            else:
                # A failed or unconfirmed local attempt must not leave a stale
                # timestamp that blocks the next delivery after reconciliation.
                order.delivered_at = None
                order.delivery_fail_reason = (error or "")[:500]


def reservation_id_of(reservation: Reservation) -> int:
    """预留一定带 id;缺失只可能来自未落库的对象。"""
    if reservation.local_id is None:  # pragma: no cover - 仓储返回值必然带 id
        msg = "预留缺少 id"
        raise RuntimeError(msg)
    return reservation.local_id
