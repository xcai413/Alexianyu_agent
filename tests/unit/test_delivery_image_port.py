"""发货侧的图片端口:常驻 Worker 与一次性 Worker 两条路径都要按失败分类返回。"""

from __future__ import annotations

import pytest

from xianyu_agent.domain.delivery import FailureKind
from xianyu_agent.runtime.account_pool import WorkerNotReadyError
from xianyu_agent.runtime.delivery_sender import make_pool_image_sender


class _Worker:
    def __init__(self, client: object) -> None:
        self._client = client


class _Pool:
    def __init__(self, *, worker: object | None = None, error: Exception | None = None) -> None:
        self._worker = worker
        self._error = error

    async def require_ready_worker(self, _account_id: str):
        if self._error is not None:
            raise self._error
        return self._worker


@pytest.mark.asyncio
async def test_pool_image_sender_reports_missing_conversation_as_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store:
        async def resolve_conversation_id(self, *, account_id: str, buyer_id: str):
            return None

    module = __import__(
        "xianyu_agent.runtime.delivery_sender", fromlist=["DomainMessageStore"]
    )
    monkeypatch.setattr(module, "DomainMessageStore", _Store)
    sender = make_pool_image_sender(_Pool(worker=_Worker(object())))

    outcome = await sender("acc-1", "B-1", "https://img.example/a.png")

    assert outcome.sent is False
    assert outcome.failure is FailureKind.NOT_SENT
    assert "会话" in (outcome.error or "")


@pytest.mark.asyncio
async def test_pool_image_sender_reports_worker_unavailable_as_not_sent() -> None:
    sender = make_pool_image_sender(
        _Pool(error=WorkerNotReadyError("worker not ready"))
    )

    outcome = await sender("acc-1", "B-1", "https://img.example/a.png")

    assert outcome.failure is FailureKind.NOT_SENT


@pytest.mark.asyncio
async def test_pool_image_sender_maps_transport_exception_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Store:
        async def resolve_conversation_id(self, *, account_id: str, buyer_id: str):
            return "chat-1"

    async def exploding(*_args, **_kwargs):
        raise RuntimeError("boom")

    module = __import__(
        "xianyu_agent.runtime.delivery_sender",
        fromlist=["DomainMessageStore", "send_worker_image"],
    )
    monkeypatch.setattr(module, "DomainMessageStore", _Store)
    monkeypatch.setattr(module, "send_worker_image", exploding)
    sender = make_pool_image_sender(_Pool(worker=_Worker(object())))

    outcome = await sender("acc-1", "B-1", "https://img.example/a.png")

    assert outcome.sent is False
    assert outcome.failure is FailureKind.UNKNOWN
    assert "结果不明" in (outcome.error or "")


@pytest.mark.asyncio
async def test_pool_image_sender_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Store:
        async def resolve_conversation_id(self, *, account_id: str, buyer_id: str):
            return "chat-9"

    seen: list[dict[str, object]] = []

    async def ok(_client, **kwargs):
        seen.append(kwargs)
        return True

    module = __import__(
        "xianyu_agent.runtime.delivery_sender",
        fromlist=["DomainMessageStore", "send_worker_image"],
    )
    monkeypatch.setattr(module, "DomainMessageStore", _Store)
    monkeypatch.setattr(module, "send_worker_image", ok)
    sender = make_pool_image_sender(_Pool(worker=_Worker(object())))

    outcome = await sender("acc-1", "B-1", "https://img.example/a.png")

    assert outcome.sent is True
    assert seen[0]["chat_id"] == "chat-9"
    assert seen[0]["image_url"] == "https://img.example/a.png"
