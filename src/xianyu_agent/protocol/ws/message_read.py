"""闲鱼已读上报(`/r/MessageStatus/read`)—— 未真实校准。

帧形状取自参考项目 xcai413/Ydisks-Xianyu-Helper(Apache-2.0,
internal/xianyu/ws/sync.go 的 `MarkChatRead`):

- lwp 是 ``/r/MessageStatus/read``;
- payload 是空对象 ``{}``;
- 参数是**一个字符串列表**,内容是待上报的平台消息 ID。

它只影响卖家侧的未读标记,不向买家发任何消息,所以风险低于发消息;但它仍然是平台写操作,
因此和发送一样"写一次 + 按响应判定",失败按明确拒绝/结果不明分开上报,不盲目重试。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeAlias

from xianyu_agent.protocol.ws.request_router import RequestRouter

ReadFrame: TypeAlias = dict[str, Any]
ReadRequest: TypeAlias = Callable[[ReadFrame], Awaitable[bool | None]]
MidFactory: TypeAlias = Callable[[], str]

READ_LWP = "/r/MessageStatus/read"
DEFAULT_TIMEOUT_S = 30.0


class MessageReadError(RuntimeError):
    """已读上报失败。"""


class MessageReadNotSent(MessageReadError):
    """可以证明请求没有写出去。"""


class MessageReadUncertain(MessageReadError):
    """请求可能已经到达平台,结果不明。"""


class MessageReadRejected(MessageReadError):
    """平台明确拒绝。"""


def generate_mid() -> str:
    """与发送帧一致的 mid 形状。"""
    return f"{uuid.uuid4().int % 1000}{int(time.time() * 1000)} 0"


def build_read_frame(
    message_ids: Iterable[str], *, mid_factory: MidFactory | None = None
) -> ReadFrame:
    """构造一次已读上报帧;空列表直接拒绝(上报空集合没有意义)。"""
    ids = [str(value).strip() for value in message_ids]
    ids = [value for value in ids if value]
    if not ids:
        msg = "已读上报至少需要一个消息 ID"
        raise ValueError(msg)
    request_id = (mid_factory or generate_mid)()
    return {
        "lwp": READ_LWP,
        "headers": {"mid": request_id},
        "body": [{}, [ids]],
    }


async def report_read(
    router: RequestRouter,
    send_request: ReadRequest,
    message_ids: Iterable[str],
    *,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    mid_factory: MidFactory | None = None,
) -> dict[str, Any]:
    """上报一次已读;失败分类与发送帧保持一致。"""
    frame = build_read_frame(message_ids, mid_factory=mid_factory)
    request_id = str(frame["headers"]["mid"])
    try:
        pending = router.register(request_id)
    except Exception as exc:
        msg = f"已读上报未能登记响应等待: {type(exc).__name__}"
        raise MessageReadNotSent(msg) from exc
    try:
        sent = await send_request(frame)
    except Exception as exc:
        router.cancel(request_id)
        msg = f"已读上报写入过程异常: {type(exc).__name__}"
        raise MessageReadUncertain(msg) from exc
    if sent is False:
        router.cancel(request_id)
        raise MessageReadNotSent("已读上报没有写出去")
    try:
        correlated = await router.wait(pending, timeout_s=timeout_s)
    except TimeoutError as exc:
        msg = "已读上报等待响应超时,结果不明"
        raise MessageReadUncertain(msg) from exc
    payload = getattr(correlated, "payload", correlated)
    if not isinstance(payload, dict):  # pragma: no cover - 防御性分支
        raise MessageReadUncertain("已读上报响应结构异常")
    code = payload.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        raise MessageReadUncertain("已读上报响应缺少明确状态码")
    if code != 200:
        raise MessageReadRejected(f"已读上报被拒绝 code={code}")
    return payload
