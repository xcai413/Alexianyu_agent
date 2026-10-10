"""发货执行器的发送端口:一次性 Worker 发送,并把"发没发出去"如实分类。

CLI 的一次性发送不能复用 daemon 的常驻连接(账号锁会拒绝),因此这条路径只在没有常驻连接时
可用;已有常驻连接时明确返回"未发送",让运营改走 daemon 执行,而不是悄悄失败。

分类规则(§16/M20):发送前的问题(锁冲突、启动失败、就绪超时)一律"明确未发送",可以安全重试;
发送调用本身抛异常或返回失败,则说明请求可能已经出去,按"结果不明"处理,冻结等人工。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from xianyu_agent.application.delivery import SendOutcome
from xianyu_agent.application.delivery.executor import (
    ConsignOutcome,
    ConsignPort,
    ImageSenderPort,
    SenderPort,
)
from xianyu_agent.application.message.send_service import SendAttemptStatus
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.infrastructure.message import DomainMessageStore
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.protocol.mtop.endpoints.consign import (
    ConsignError,
    ConsignRejectedError,
    XianyuConsignClient,
)
from xianyu_agent.runtime.account_lock import AccountConnectionAlreadyRunningError
from xianyu_agent.runtime.account_pool import WorkerNotReadyError
from xianyu_agent.runtime.message_sender import send_worker_delivery_text, send_worker_image
from xianyu_agent.services.account_worker import AccountWorker

CONNECT_TIMEOUT_S = 15.0
CONNECT_POLL_S = 0.05


class PreSendUnavailable(RuntimeError):
    """明确的发送前不可用:这条消息没有写出去。"""


def make_pool_image_sender(pool: Any) -> ImageSenderPort:
    """Build the daemon-side image port from a live account pool.

    图片必须走已校准的会话信封(需要 receiver),所以这里复用常驻 Worker 的连接与消息存储;
    发送前不可用一律"未发送"(可重试),写入过程中的异常一律"结果不明"。
    """

    async def _send(account_id: str, buyer_id: str, image_url: str) -> SendOutcome:
        try:
            worker = await pool.require_ready_worker(account_id)
        except WorkerNotReadyError as exc:
            return SendOutcome.not_sent(str(exc))
        store = DomainMessageStore()
        try:
            conversation_id = await store.resolve_conversation_id(
                account_id=account_id, buyer_id=buyer_id
            )
        except Exception as exc:
            return SendOutcome.not_sent(f"会话解析失败({type(exc).__name__})")
        if conversation_id is None:
            return SendOutcome.not_sent(
                "该买家还没有会话记录,无法定位聊天会话;先让买家发一条消息或改用文本池"
            )
        try:
            sent = await send_worker_image(
                worker._client,
                account_id=account_id,
                chat_id=conversation_id,
                image_url=image_url,
            )
        except Exception as exc:
            return SendOutcome.unknown(f"图片发送过程中异常({type(exc).__name__}),结果不明")
        if sent:
            return SendOutcome.success()
        return SendOutcome.unknown("图片通道返回失败,无法确认对方是否收到")

    return _send


def make_consign_port(client: XianyuConsignClient | None = None) -> ConsignPort:
    """Build the platform-confirmation port used after the card was delivered.

    这个动作发生在内容已经发给买家之后:平台明确拒绝与结果不明都会让任务停在待人工
    处理的状态,调用方不会因此重发卡密。凭证文本只用于请求,不进入日志或错误串。
    """
    resolved = client or XianyuConsignClient()

    async def _confirm(account_id: str, order_id: str, credential: str) -> ConsignOutcome:
        try:
            await resolved.confirm_delivery(
                account_id, order_id, trade_text=credential
            )
        except ConsignRejectedError as exc:
            return ConsignOutcome.rejected(str(exc))
        except ConsignError as exc:
            return ConsignOutcome.unknown(str(exc))
        except Exception as exc:  # 端口异常也不能被当成确认成功
            return ConsignOutcome.unknown(f"平台确认调用异常({type(exc).__name__})")
        return ConsignOutcome.success()

    return _confirm


def make_pool_sender(pool: Any) -> SenderPort:
    """Build the daemon-side delivery port from a live account pool.

    Reuses the Worker that already owns the account WebSocket, so a delivery
    triggered inside the daemon never opens a second session. Classification
    mirrors the one-shot CLI path: every pre-send condition is "not sent"
    (safe to retry), while an exception or a failed send call is "unknown"
    (the request may have left the process).
    """
    async def _send(account_id: str, order_id: str, text: str) -> SendOutcome:
        try:
            worker = await pool.require_ready_worker(account_id)
        except WorkerNotReadyError as exc:
            return SendOutcome.not_sent(str(exc))
        try:
            result = await send_worker_delivery_text(
                worker._client,
                account_id=account_id,
                order_id=order_id,
                text=text,
            )
        except Exception as exc:
            return SendOutcome.unknown(f"发送过程中异常({type(exc).__name__}),结果不明")
        if result.status is SendAttemptStatus.SUCCESS:
            return SendOutcome.success(result.client_message_id)
        if result.status is SendAttemptStatus.FAILED_RETRYABLE:
            return SendOutcome.not_sent(result.detail or "消息发送前失败")
        if result.status is SendAttemptStatus.FAILED_FINAL:
            return SendOutcome.rejected(result.detail or "平台拒绝发送")
        return SendOutcome.unknown(result.detail or "消息发送结果不明")

    return _send


async def _wait_for_connection(
    worker: AccountWorker, *, timeout_s: float = CONNECT_TIMEOUT_S
) -> None:
    """等待协议会话可用;任何确定的失败都抛 PreSendUnavailable。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout_s)
    while True:
        lifecycle_error = worker.lifecycle_error
        if lifecycle_error is not None:
            msg = f"Worker 生命周期失败({type(lifecycle_error).__name__})"
            raise PreSendUnavailable(msg)
        if worker.state is WorkerState.NEEDS_VALIDATION:
            msg = "账号需要人工验证"
            raise PreSendUnavailable(msg)
        if worker.state is WorkerState.ERROR:
            msg = "账号 Worker 已进入 ERROR"
            raise PreSendUnavailable(msg)
        if worker._client.state is ConnectionState.CONNECTED:
            return
        if loop.time() >= deadline:
            msg = "等待 WebSocket 就绪超时"
            raise TimeoutError(msg)
        await asyncio.sleep(CONNECT_POLL_S)


async def send_once(account_id: str, order_id: str, text: str) -> SendOutcome:
    """Send one delivery message through a one-shot worker."""
    del order_id  # 发货消息走账号的默认会话,订单号只用于调用方追踪
    worker = AccountWorker(account_id)
    try:
        try:
            startup = worker.start()
        except AccountConnectionAlreadyRunningError:
            return SendOutcome.not_sent(
                "该账号已有常驻 WebSocket 连接:请由 daemon 执行发货,CLI 一次性发送不可用"
            )
        if startup is not None:
            try:
                await startup
            except Exception as exc:
                return SendOutcome.not_sent(f"Worker 启动失败({type(exc).__name__})")
        try:
            await _wait_for_connection(worker)
        except (PreSendUnavailable, TimeoutError) as exc:
            return SendOutcome.not_sent(str(exc))
        try:
            sent = await worker.send_text(text)
        except Exception as exc:
            return SendOutcome.unknown(f"发送过程中异常({type(exc).__name__}),结果不明")
        if sent:
            return SendOutcome.success()
        return SendOutcome.unknown("发送通道返回失败,无法确认对方是否收到")
    finally:
        with contextlib.suppress(Exception):
            await worker.stop()


async def send_once_image(  # noqa: PLR0911 - 每个失败分类都要如实返回,提前返回最清楚
    account_id: str, buyer_id: str, image_url: str
) -> SendOutcome:
    """Send one delivery image through a one-shot worker.

    图片消息必须有真实会话(receiver),所以这里先按买家反查会话;查不到就明确报"未发送",
    绝不猜一个 chat id 冒险发出去。
    """
    worker = AccountWorker(account_id)
    try:
        try:
            startup = worker.start()
        except AccountConnectionAlreadyRunningError:
            return SendOutcome.not_sent(
                "该账号已有常驻 WebSocket 连接:请由 daemon 执行发货,CLI 一次性发送不可用"
            )
        if startup is not None:
            try:
                await startup
            except Exception as exc:
                return SendOutcome.not_sent(f"Worker 启动失败({type(exc).__name__})")
        try:
            await _wait_for_connection(worker)
        except (PreSendUnavailable, TimeoutError) as exc:
            return SendOutcome.not_sent(str(exc))
        store = DomainMessageStore()
        try:
            conversation_id = await store.resolve_conversation_id(
                account_id=account_id, buyer_id=buyer_id
            )
        except Exception as exc:
            return SendOutcome.not_sent(f"会话解析失败({type(exc).__name__})")
        if conversation_id is None:
            return SendOutcome.not_sent("该买家还没有会话记录,无法定位聊天会话")
        try:
            sent = await send_worker_image(
                worker._client,
                account_id=account_id,
                chat_id=conversation_id,
                image_url=image_url,
            )
        except Exception as exc:
            return SendOutcome.unknown(f"图片发送过程中异常({type(exc).__name__}),结果不明")
        if sent:
            return SendOutcome.success()
        return SendOutcome.unknown("图片通道返回失败,无法确认对方是否收到")
    finally:
        with contextlib.suppress(Exception):
            await worker.stop()
