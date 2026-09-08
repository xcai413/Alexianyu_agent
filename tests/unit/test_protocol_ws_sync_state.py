"""Contract and isolation tests for the canonical WS sync-state exchange."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import sync
from xianyu_agent.protocol.ws.decoder import DecodedFrame
from xianyu_agent.protocol.ws.request_router import RequestRouter


def _mid_sequence(*values: str) -> Callable[[], str]:
    iterator = iter(values)
    return lambda: next(iterator)


async def _wait_for_sent(sent: list[sync.SyncFrame], count: int) -> None:
    for _ in range(100):
        if len(sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} sent frames, got {len(sent)}")


def test_sync_extra_type_trigger_contract() -> None:
    canonical = WsFrame(body={"syncExtraType": {"type": "2"}})

    assert sync.extract_sync_extra_type({"body": {"syncExtraType": {"type": 1}}}) == 1
    assert sync.extract_sync_extra_type({"body": {"syncExtraType": {"type": "2"}}}) == 2
    assert sync.extract_sync_extra_type(canonical) == 2
    assert sync.requires_state_sync(canonical) is True
    assert sync.requires_state_sync({"body": {"syncExtraType": {"type": 1}}}) is True
    assert sync.requires_state_sync({"body": {"syncExtraType": {"type": 2.0}}}) is True
    assert sync.requires_state_sync({"body": {"syncExtraType": {"type": 9}}}) is False
    assert sync.requires_state_sync({"body": {}}) is False
    assert sync.requires_state_sync(None) is False


def test_get_state_request_matches_wire_contract() -> None:
    frame = sync.build_get_state_request(mid_factory=lambda: "get-mid")

    assert frame == {
        "lwp": "/r/SyncStatus/getState",
        "headers": {"mid": "get-mid"},
        "body": [{"topic": "sync"}],
    }


def test_ack_diff_request_uses_exact_get_state_body() -> None:
    state_body = {"pipeline": "sync", "pts": 123, "nested": {"value": 1}}

    frame = sync.build_ack_diff_request(state_body, mid_factory=lambda: "ack-mid")

    assert frame == {
        "lwp": "/r/SyncStatus/ackDiff",
        "headers": {"mid": "ack-mid"},
        "body": [state_body],
    }
    assert frame["body"][0] is state_body


def test_sync_request_builders_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="body must not be None"):
        sync.build_ack_diff_request(None)
    with pytest.raises(ValueError, match="empty request id"):
        sync.build_get_state_request(mid_factory=lambda: "")


@pytest.mark.asyncio
async def test_unsupported_sync_extra_is_noop() -> None:
    router = RequestRouter()
    sent: list[sync.SyncFrame] = []

    async def send_request(frame: sync.SyncFrame) -> bool:
        sent.append(frame)
        return True

    result = await sync.handle_sync_extra(
        router,
        send_request,
        {"body": {"syncExtraType": {"type": 9}}},
    )

    assert result is None
    assert sent == []
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_sync_extra_registers_before_send_and_accepts_decoded_router_response() -> None:
    router = RequestRouter()
    state_body = {"pipeline": "sync", "pts": 456}
    sent: list[sync.SyncFrame] = []

    async def send_request(frame: sync.SyncFrame) -> bool:
        sent.append(frame)
        assert router.pending_count == 1
        request_id = str(frame["headers"]["mid"])
        if frame["lwp"] == sync.GET_STATE_LWP:
            payload = {"code": "200", "body": state_body}
        else:
            assert frame["body"] == [state_body]
            payload = {"code": 200}
        decoded = DecodedFrame(
            frame=WsFrame(headers={"mid": request_id}),
            payload=payload,
            raw_text="{}",
        )
        assert router.match(request_id, decoded) is True
        return True

    result = await sync.handle_sync_extra(
        router,
        send_request,
        WsFrame(body={"syncExtraType": {"type": 1}}),
        timeout_s=0.1,
        mid_factory=_mid_sequence("get-mid", "ack-mid"),
    )

    assert result == sync.SubscriptionReady(sync_type=1, state_body=state_body)
    assert [frame["lwp"] for frame in sent] == [sync.GET_STATE_LWP, sync.ACK_DIFF_LWP]
    assert router.pending_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"code": 500, "body": {"pts": 1}},
        {"code": "not-a-code", "body": {"pts": 1}},
        {"code": 200, "body": None},
    ],
)
async def test_invalid_get_state_response_stops_before_ack_diff(response: dict) -> None:
    router = RequestRouter()
    sent: list[sync.SyncFrame] = []

    async def send_request(frame: sync.SyncFrame) -> bool:
        sent.append(frame)
        assert router.match(str(frame["headers"]["mid"]), response) is True
        return True

    with pytest.raises(sync.SyncStateError, match="getState response invalid"):
        await sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            timeout_s=0.1,
            mid_factory=lambda: "get-mid",
        )

    assert [frame["lwp"] for frame in sent] == [sync.GET_STATE_LWP]
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_non_mapping_get_state_response_is_rejected() -> None:
    router = RequestRouter()

    async def send_request(frame: sync.SyncFrame) -> bool:
        assert router.match(str(frame["headers"]["mid"]), "bad-response") is True
        return True

    with pytest.raises(sync.SyncStateError, match="getState response must be a mapping"):
        await sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            timeout_s=0.1,
            mid_factory=lambda: "get-mid",
        )

    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_ack_diff_rejects_explicit_non_200_code() -> None:
    router = RequestRouter()

    async def send_request(frame: sync.SyncFrame) -> bool:
        request_id = str(frame["headers"]["mid"])
        if frame["lwp"] == sync.GET_STATE_LWP:
            assert router.match(request_id, {"code": 200, "body": {"pts": 1}}) is True
        else:
            assert router.match(request_id, {"code": "503"}) is True
        return True

    with pytest.raises(sync.SyncStateError, match="ackDiff response invalid: code=503"):
        await sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 2}}},
            timeout_s=0.1,
            mid_factory=_mid_sequence("get-mid", "ack-mid"),
        )

    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_ack_diff_missing_code_preserves_reference_success_semantics() -> None:
    router = RequestRouter()
    state_body = {"pts": 1}

    async def send_request(frame: sync.SyncFrame) -> bool:
        request_id = str(frame["headers"]["mid"])
        response = (
            {"code": 200, "body": state_body}
            if frame["lwp"] == sync.GET_STATE_LWP
            else {"body": {}}
        )
        assert router.match(request_id, response) is True
        return True

    result = await sync.handle_sync_extra(
        router,
        send_request,
        {"body": {"syncExtraType": {"type": 2}}},
        timeout_s=0.1,
        mid_factory=_mid_sequence("get-mid", "ack-mid"),
    )

    assert result == sync.SubscriptionReady(sync_type=2, state_body=state_body)
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_concurrent_sync_exchanges_are_isolated_when_responses_arrive_out_of_order() -> None:
    router = RequestRouter()
    sent: list[sync.SyncFrame] = []

    async def send_request(frame: sync.SyncFrame) -> bool:
        sent.append(frame)
        return True

    first = asyncio.create_task(
        sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            timeout_s=0.2,
            mid_factory=_mid_sequence("first-get", "first-ack"),
        )
    )
    second = asyncio.create_task(
        sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 2}}},
            timeout_s=0.2,
            mid_factory=_mid_sequence("second-get", "second-ack"),
        )
    )

    await _wait_for_sent(sent, 2)
    assert router.pending_count == 2
    assert router.match("second-get", {"code": 200, "body": {"owner": "second"}}) is True
    assert router.match("first-get", {"code": 200, "body": {"owner": "first"}}) is True

    await _wait_for_sent(sent, 4)
    ack_frames = {str(frame["headers"]["mid"]): frame for frame in sent[2:]}
    assert ack_frames["first-ack"]["body"] == [{"owner": "first"}]
    assert ack_frames["second-ack"]["body"] == [{"owner": "second"}]
    assert router.pending_count == 2

    assert router.match("first-ack", {"code": 200}) is True
    assert router.match("second-ack", {"code": 200}) is True

    assert await asyncio.gather(first, second) == [
        sync.SubscriptionReady(sync_type=1, state_body={"owner": "first"}),
        sync.SubscriptionReady(sync_type=2, state_body={"owner": "second"}),
    ]
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_get_state_timeout_cleans_pending_and_late_response_is_safe() -> None:
    router = RequestRouter()

    async def send_request(_frame: sync.SyncFrame) -> bool:
        return True

    with pytest.raises(TimeoutError):
        await sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            timeout_s=0.01,
            mid_factory=lambda: "get-timeout",
        )

    assert router.pending_count == 0
    assert router.match("get-timeout", {"code": 200}) is False


@pytest.mark.asyncio
async def test_ack_diff_timeout_cleans_pending_and_late_response_is_safe() -> None:
    router = RequestRouter()
    sent: list[sync.SyncFrame] = []

    async def send_request(frame: sync.SyncFrame) -> bool:
        sent.append(frame)
        if frame["lwp"] == sync.GET_STATE_LWP:
            assert router.match(
                str(frame["headers"]["mid"]),
                {"code": 200, "body": {"pts": 1}},
            ) is True
        return True

    with pytest.raises(TimeoutError):
        await sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            timeout_s=0.01,
            mid_factory=_mid_sequence("get-mid", "ack-timeout"),
        )

    assert [frame["lwp"] for frame in sent] == [sync.GET_STATE_LWP, sync.ACK_DIFF_LWP]
    assert router.pending_count == 0
    assert router.match("ack-timeout", {"code": 200}) is False


@pytest.mark.asyncio
async def test_cancellation_while_waiting_cleans_pending_state() -> None:
    router = RequestRouter()
    sent: list[sync.SyncFrame] = []

    async def send_request(frame: sync.SyncFrame) -> bool:
        sent.append(frame)
        return True

    task = asyncio.create_task(
        sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            timeout_s=None,
            mid_factory=lambda: "cancel-wait",
        )
    )
    await _wait_for_sent(sent, 1)
    assert router.pending_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert router.pending_count == 0
    assert router.match("cancel-wait", {"code": 200}) is False


@pytest.mark.asyncio
async def test_cancellation_during_send_cleans_pending_state() -> None:
    router = RequestRouter()
    entered_send = asyncio.Event()
    release_send = asyncio.Event()

    async def send_request(_frame: sync.SyncFrame) -> bool:
        entered_send.set()
        await release_send.wait()
        return True

    task = asyncio.create_task(
        sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            timeout_s=None,
            mid_factory=lambda: "cancel-send",
        )
    )
    await entered_send.wait()
    assert router.pending_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert router.pending_count == 0
    assert router.match("cancel-send", {"code": 200}) is False


@pytest.mark.asyncio
async def test_send_false_cleans_pending_and_raises_connection_error() -> None:
    router = RequestRouter()

    async def send_request(_frame: sync.SyncFrame) -> bool:
        return False

    with pytest.raises(ConnectionError, match="sync-state request send failed"):
        await sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            mid_factory=lambda: "send-false",
        )

    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_send_exception_cleans_pending_and_propagates() -> None:
    router = RequestRouter()

    async def send_request(_frame: sync.SyncFrame) -> bool:
        raise RuntimeError("send exploded")

    with pytest.raises(RuntimeError, match="send exploded"):
        await sync.handle_sync_extra(
            router,
            send_request,
            {"body": {"syncExtraType": {"type": 1}}},
            mid_factory=lambda: "send-error",
        )

    assert router.pending_count == 0
