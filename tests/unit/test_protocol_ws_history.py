"""Isolation and contract tests for the M5 WebSocket history primitive."""

from __future__ import annotations

import asyncio

import pytest

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import history
from xianyu_agent.protocol.ws.request_router import RequestRouter


def test_conversation_list_request_matches_wire_contract_and_defaults() -> None:
    frame = history.build_conversation_list_request(mid_factory=lambda: "conv-mid")

    assert frame == {
        "lwp": "/r/Conversation/listNewestPagination",
        "headers": {"mid": "conv-mid"},
        "body": [9_007_199_254_740_991, 100],
    }


def test_conversation_list_request_preserves_valid_pagination_and_clamps_limit() -> None:
    assert history.build_conversation_list_request(
        cursor=123,
        limit=20,
        mid_factory=lambda: "m-1",
    )["body"] == [123, 20]
    assert history.build_conversation_list_request(
        cursor=-1,
        limit=101,
        mid_factory=lambda: "m-2",
    )["body"] == [history.NEWEST_CURSOR, history.DEFAULT_CONVERSATION_LIMIT]


def test_message_history_request_matches_wire_contract_and_defaults() -> None:
    frame = history.build_message_history_request(" cid-1 ", mid_factory=lambda: "history-mid")

    assert frame == {
        "lwp": "/r/MessageManager/listUserMessages",
        "headers": {"mid": "history-mid"},
        "body": ["cid-1", False, 9_007_199_254_740_991, 50, False],
    }


def test_message_history_request_rejects_empty_conversation_id() -> None:
    with pytest.raises(ValueError, match="conversation id"):
        history.build_message_history_request("  ")


def test_history_builder_rejects_empty_generated_mid() -> None:
    with pytest.raises(ValueError, match="empty request id"):
        history.build_conversation_list_request(mid_factory=lambda: "")


@pytest.mark.asyncio
async def test_request_registers_before_send_and_matches_frame_response() -> None:
    router = RequestRouter()

    async def send_request(frame: history.HistoryFrame) -> bool:
        assert router.pending_count == 1
        response = {"code": 200, "body": {"items": ["conversation"]}}
        matched = router.match_frame(WsFrame(headers={"mid": frame["headers"]["mid"]}), response)
        assert matched is True
        return True

    response = await history.request_conversations(
        router,
        send_request,
        timeout_s=0.1,
        mid_factory=lambda: "conv-mid",
    )

    assert response == {"code": 200, "body": {"items": ["conversation"]}}
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_conversation_and_message_requests_are_isolated_out_of_order() -> None:
    router = RequestRouter()
    sent: list[history.HistoryFrame] = []

    async def send_request(frame: history.HistoryFrame) -> bool:
        sent.append(frame)
        return True

    conversations = asyncio.create_task(
        history.request_conversations(
            router,
            send_request,
            timeout_s=0.2,
            mid_factory=lambda: "conv-mid",
        )
    )
    messages = asyncio.create_task(
        history.request_message_history(
            router,
            send_request,
            "cid-1",
            timeout_s=0.2,
            mid_factory=lambda: "msg-mid",
        )
    )

    for _ in range(10):
        if len(sent) == 2:
            break
        await asyncio.sleep(0)

    assert {frame["lwp"] for frame in sent} == {
        history.CONVERSATION_LIST_LWP,
        history.MESSAGE_HISTORY_LWP,
    }
    assert router.pending_count == 2
    assert router.match("msg-mid", {"kind": "messages"}) is True
    assert router.match("conv-mid", {"kind": "conversations"}) is True

    assert await asyncio.gather(conversations, messages) == [
        {"kind": "conversations"},
        {"kind": "messages"},
    ]
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_history_request_timeout_cleans_router_state() -> None:
    router = RequestRouter()

    async def send_request(_frame: history.HistoryFrame) -> bool:
        return True

    with pytest.raises(TimeoutError):
        await history.request_conversations(
            router,
            send_request,
            timeout_s=0.01,
            mid_factory=lambda: "timeout-mid",
        )

    assert router.pending_count == 0
    assert router.match("timeout-mid", object()) is False


@pytest.mark.asyncio
async def test_cancellation_during_send_cleans_router_state() -> None:
    router = RequestRouter()
    entered_send = asyncio.Event()
    release_send = asyncio.Event()

    async def send_request(_frame: history.HistoryFrame) -> bool:
        entered_send.set()
        await release_send.wait()
        return True

    task = asyncio.create_task(
        history.request_message_history(
            router,
            send_request,
            "cid-1",
            timeout_s=None,
            mid_factory=lambda: "cancel-mid",
        )
    )
    await entered_send.wait()
    assert router.pending_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert router.pending_count == 0
    assert router.match("cancel-mid", object()) is False


@pytest.mark.asyncio
async def test_send_false_cleans_pending_and_raises_connection_error() -> None:
    router = RequestRouter()

    async def send_request(_frame: history.HistoryFrame) -> bool:
        return False

    with pytest.raises(ConnectionError, match="history request send failed"):
        await history.request_conversations(
            router,
            send_request,
            mid_factory=lambda: "send-false-mid",
        )

    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_send_exception_cleans_pending_and_propagates() -> None:
    router = RequestRouter()

    async def send_request(_frame: history.HistoryFrame) -> bool:
        raise RuntimeError("send exploded")

    with pytest.raises(RuntimeError, match="send exploded"):
        await history.request_message_history(
            router,
            send_request,
            "cid-1",
            mid_factory=lambda: "send-error-mid",
        )

    assert router.pending_count == 0
